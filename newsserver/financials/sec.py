"""미국 — SEC XBRL ``companyfacts`` (10-K·10-Q 를 내는 회사의 us-gaap 사실).

회사 하나가 한 번의 호출이다. 필요한 태그만 추려 메모리에 둔다.

기간 식별
  회계연도  10-K 의 12개월(340~390일) 기간. 표기(fy)는 그 기간을 처음 보고한 10-K 의 것
            (회사마다 결산월·표기 관례가 달라 날짜로 추정하지 않는다). 아직 10-K 가 없는
            진행 중인 회계연도는 직전 회계연도 종료 다음 날부터로 본다.
  분기      80~100일 기간. 분기 번호는 회계연도 시작일로부터의 위치로 정한다
            (옛 10-K 안의 분기 값은 fp 가 FY 로 표기돼 있어 fp 를 쓰지 않는다).

분기 값 계산
  손익      3개월 사실. 없으면 누적(YTD) 차감. 4분기 = 연간 − 3분기 누적
  현금흐름  10-Q 는 누적만 보고하므로 누적 차감 (2~4분기는 계산값)
  재무상태  기간 말 시점 사실
같은 기간을 여러 공시가 보고하면(비교 기간·정정) 가장 늦게 공시된 값을 쓴다.
태그는 기간마다 우선순위대로 찾고, 차감 계산은 두 값이 같은 태그일 때만 한다.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import httpx

from newsserver.config import Settings
from newsserver.financials.cache import MemoryCache
from newsserver.financials.model import (
    ITEM_KIND, FinancialPeriod, NotConfigured, UpstreamError, add_free_cash_flow,
)

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# 항목 → us-gaap 태그 후보 (우선순위 순)
TAGS: dict[str, tuple[str, ...]] = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "RevenuesNetOfInterestExpense"),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "pretax_income": ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
    "net_income": ("ProfitLoss", "NetIncomeLoss"),
    "net_income_attributable": ("NetIncomeLoss",),
    "eps_basic": ("EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted"),
    "eps_diluted": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
    "total_assets": ("Assets",),
    "current_assets": ("AssetsCurrent",),
    "cash_and_equivalents": ("CashAndCashEquivalentsAtCarryingValue",
                             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "total_liabilities": ("Liabilities",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "total_equity": ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "StockholdersEquity"),
    "equity_attributable": ("StockholdersEquity",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
    "dividends_paid": ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
    "shares_outstanding": ("CommonStockSharesOutstanding",),
}
INCOME_ITEMS = ("revenue", "gross_profit", "operating_income", "pretax_income", "net_income",
                "net_income_attributable", "eps_basic", "eps_diluted")
CASH_ITEMS = ("operating_cash_flow", "capex", "dividends_paid")
STOCK_ITEMS = ("total_assets", "current_assets", "cash_and_equivalents", "total_liabilities", "current_liabilities",
               "total_equity", "equity_attributable", "shares_outstanding")
OUTFLOW_ITEMS = {"capex", "dividends_paid"}
EXTRA_TAGS = ("LiabilitiesAndStockholdersEquity",)
UNITS = {"eps_basic": "USD/shares", "eps_diluted": "USD/shares", "shares_outstanding": "shares"}
# 기간 식별에 쓰는 태그 (거의 모든 회사가 보고한다)
PERIOD_TAGS = ("NetIncomeLoss", "ProfitLoss", *TAGS["revenue"], "OperatingIncomeLoss")

ANNUAL_DAYS, QUARTER_DAYS, TOL = (340, 390), (80, 100), dt.timedelta(days=3)


@dataclass(frozen=True)
class Fact:
    start: dt.date | None
    end: dt.date
    val: float
    form: str
    filed: str
    fy: int | None
    accn: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days if self.start else 0


@dataclass
class CompanyFacts:
    name: str
    tags: dict[str, list[Fact]]
    dei_shares: list[Fact] = field(default_factory=list)

    def latest(self, tag: str, start: dt.date | None, end: dt.date, *, fuzzy_start: bool = False) -> Fact | None:
        best = None
        for f in self.tags.get(tag, ()):
            if f.end != end:
                continue
            if start is None:
                if f.start is not None:
                    continue
            elif f.start is None or (f.start != start and not (fuzzy_start and abs(f.start - start) <= TOL)):
                continue
            if best is None or f.filed > best.filed:
                best = f
        return best


def _facts(data: dict, tag: str, unit: str) -> list[Fact]:
    out = []
    for x in (data.get(tag) or {}).get("units", {}).get(unit, []):
        form = str(x.get("form") or "")
        if not form.startswith(("10-K", "10-Q")):
            continue
        try:
            out.append(Fact(start=dt.date.fromisoformat(x["start"]) if x.get("start") else None,
                            end=dt.date.fromisoformat(x["end"]), val=x["val"], form=form, filed=str(x.get("filed")),
                            fy=x.get("fy"), accn=str(x.get("accn") or "")))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def parse_company_facts(doc: dict) -> CompanyFacts:
    gaap = (doc.get("facts") or {}).get("us-gaap") or {}
    tags: dict[str, list[Fact]] = {}
    for item, candidates in TAGS.items():
        for tag in candidates:
            if tag not in tags:
                tags[tag] = _facts(gaap, tag, UNITS.get(item, "USD"))
    for tag in EXTRA_TAGS:
        tags[tag] = _facts(gaap, tag, "USD")
    dei = (doc.get("facts") or {}).get("dei") or {}
    shares = []
    for x in (dei.get("EntityCommonStockSharesOutstanding") or {}).get("units", {}).get("shares", []):
        try:
            shares.append(Fact(None, dt.date.fromisoformat(x["end"]), x["val"], str(x.get("form") or ""),
                               str(x.get("filed")), x.get("fy"), str(x.get("accn") or "")))
        except (KeyError, ValueError):
            continue
    return CompanyFacts(name=str(doc.get("entityName") or ""), tags=tags, dei_shares=shares)


@dataclass
class FiscalYear:
    label: int
    start: dt.date
    end: dt.date | None      # 진행 중이면 None
    annual: Fact | None      # 10-K 의 연간 사실 (공시 정보용)


class SecFinancials:
    source = "sec"

    def __init__(self, settings: Settings, http: httpx.AsyncClient, cache: MemoryCache):
        self.settings = settings
        self.http = http
        self.cache = cache

    def _check(self) -> None:
        if not self.settings.edgar_user_agent:
            raise NotConfigured("EDGAR_USER_AGENT 미설정")

    async def _document(self, cik: int) -> dict | None:
        try:
            resp = await self.http.get(FACTS_URL.format(cik=cik),
                                       headers={"User-Agent": self.settings.edgar_user_agent}, timeout=30)
        except httpx.HTTPError as e:
            raise UpstreamError(f"SEC 요청 실패: {type(e).__name__}") from e
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise UpstreamError(f"SEC HTTP {resp.status_code}")
        return resp.json()

    async def facts(self, cik: int) -> CompanyFacts | None:
        async def load() -> CompanyFacts | None:
            doc = await self._document(cik)
            return parse_company_facts(doc) if doc else None
        return await self.cache.get_or_load(("sec", "facts", cik), load)

    # ── 기간 조립 ────────────────────────────────────────────────────────────
    async def periods(self, cik: int, *, period: str, limit: int, raw: bool) -> tuple[list[FinancialPeriod], str | None]:
        self._check()
        cf = await self.facts(cik)
        if cf is None:
            return [], None
        years = fiscal_years(cf)
        built = (annual_periods(cf, years) if period == "annual" else quarter_periods(cf, years))[:limit]
        if raw and built:
            doc = await self._document(cik)  # 원본 전체는 크기 때문에 캐시하지 않는다
            if doc:
                for p in built:
                    p.raw = raw_facts(doc, p)
        return built, ("USD" if built else None)


def fiscal_years(cf: CompanyFacts) -> list[FiscalYear]:
    windows: dict[tuple[dt.date, dt.date], Fact] = {}
    for tag in PERIOD_TAGS:
        for f in cf.tags.get(tag, ()):
            if f.start and f.form.startswith("10-K") and ANNUAL_DAYS[0] <= f.days <= ANNUAL_DAYS[1]:
                key = (f.start, f.end)
                if key not in windows or f.filed < windows[key].filed:
                    windows[key] = f
    years: list[FiscalYear] = []
    for (start, end), first in sorted(windows.items(), key=lambda kv: kv[0][1]):
        if years and years[-1].end and abs(years[-1].end - end) <= TOL:
            continue  # 같은 회계연도를 조금 다른 날짜로 보고한 경우
        years.append(FiscalYear(label=int(first.fy) if first.fy else end.year, start=start, end=end, annual=first))
    # 아직 10-K 가 없는 진행 중인 회계연도
    latest_end = max((f.end for t in PERIOD_TAGS for f in cf.tags.get(t, ()) if f.form.startswith("10-Q")), default=None)
    if years and latest_end and latest_end > years[-1].end + TOL:
        years.append(FiscalYear(label=years[-1].label + 1, start=years[-1].end + dt.timedelta(days=1), end=None,
                                annual=None))
    return years


def _quarter_number(fy: FiscalYear, end: dt.date) -> int:
    return max(1, min(4, round((end - fy.start).days / 91.3)))


def _pick(cf: CompanyFacts, item: str, start: dt.date | None, end: dt.date, *, fuzzy: bool = False) -> tuple[str, Fact] | None:
    for tag in TAGS[item]:
        f = cf.latest(tag, start, end, fuzzy_start=fuzzy)
        if f is not None:
            return tag, f
    return None


def _diff(cf: CompanyFacts, item: str, a: tuple[dt.date, dt.date], b: tuple[dt.date, dt.date]) -> float | None:
    """a − b 를 같은 태그로 계산한다 (예: 연간 − 3분기 누적)."""
    for tag in TAGS[item]:
        fa = cf.latest(tag, a[0], a[1], fuzzy_start=True)
        fb = cf.latest(tag, b[0], b[1], fuzzy_start=True)
        if fa is not None and fb is not None:
            return fa.val - fb.val
    return None


def _num(v: float | None) -> float | int | None:
    if v is None:
        return None
    return int(v) if float(v).is_integer() else v


def _stocks(cf: CompanyFacts, p: FinancialPeriod, end: dt.date, accn: str | None) -> None:
    for item in STOCK_ITEMS:
        hit = _pick(cf, item, None, end)
        p.items[item] = hit[1].val if hit else None
    if p.items.get("total_liabilities") is None:
        total = cf.latest("LiabilitiesAndStockholdersEquity", None, end)
        equity = p.items.get("total_equity")
        if total is not None and equity is not None:
            p.items["total_liabilities"] = total.val - equity
            p.derived.append("total_liabilities")
    if p.items.get("shares_outstanding") is None and accn:
        # 기간 말 값이 없으면 같은 공시 표지의 기준일 값
        cover = [f for f in cf.dei_shares if f.accn == accn]
        if cover:
            p.items["shares_outstanding"] = max(cover, key=lambda f: f.end).val


def _filing(f: Fact | None) -> dict[str, Any]:
    if f is None:
        return {"id": None, "form": None, "filed": None}
    return {"id": f.accn, "form": f.form, "filed": f.filed}


def _first_report(cf: CompanyFacts, start: dt.date, end: dt.date) -> Fact | None:
    """그 기간을 처음 보고한 공시 (원래 분기·연간 보고서)."""
    best = None
    for tag in PERIOD_TAGS:
        for f in cf.tags.get(tag, ()):
            if f.end == end and f.start and abs(f.start - start) <= TOL and (best is None or f.filed < best.filed):
                best = f
    return best


def _finish(p: FinancialPeriod) -> FinancialPeriod:
    for item in OUTFLOW_ITEMS:
        if p.items.get(item) is not None:
            p.items[item] = abs(p.items[item])
    p.items = {k: (round(v, 4) if v is not None and ITEM_KIND[k] == "per_share" else _num(v))
               for k, v in p.items.items()}
    p.derived = [k for k in dict.fromkeys(p.derived) if p.items.get(k) is not None]
    add_free_cash_flow(p)
    return p


def annual_periods(cf: CompanyFacts, years: list[FiscalYear]) -> list[FinancialPeriod]:
    out = []
    for fy in reversed(years):
        if fy.end is None:
            continue
        p = FinancialPeriod(fiscal_year=fy.label, fiscal_quarter=None, start=fy.start.isoformat(),
                            end=fy.end.isoformat(), basis="consolidated", filing=_filing(fy.annual))
        for item in (*INCOME_ITEMS, *CASH_ITEMS):
            hit = _pick(cf, item, fy.start, fy.end, fuzzy=True)
            p.items[item] = hit[1].val if hit else None
        _stocks(cf, p, fy.end, fy.annual.accn if fy.annual else None)
        out.append(_finish(p))
    return out


def quarter_periods(cf: CompanyFacts, years: list[FiscalYear]) -> list[FinancialPeriod]:
    out: list[FinancialPeriod] = []
    for fy in reversed(years):
        # 이 회계연도의 분기 종료일 — 3개월 사실과 누적 사실의 종료일
        ends: dict[int, dt.date] = {}
        for tag in PERIOD_TAGS:
            for f in cf.tags.get(tag, ()):
                if not f.start or f.end <= fy.start or (fy.end and f.end > fy.end + TOL):
                    continue
                three_month = QUARTER_DAYS[0] <= f.days <= QUARTER_DAYS[1] and f.start >= fy.start - TOL
                ytd = abs(f.start - fy.start) <= TOL and f.days < ANNUAL_DAYS[0]
                if three_month or ytd:
                    ends.setdefault(_quarter_number(fy, f.end), f.end)
        if fy.end is not None:
            ends[4] = fy.end
        for q in sorted(ends, reverse=True):
            end = ends[q]
            prev_end = ends.get(q - 1)
            start = fy.start if q == 1 else (prev_end + dt.timedelta(days=1) if prev_end else None)
            if start is None:
                continue
            p = FinancialPeriod(fiscal_year=fy.label, fiscal_quarter=q, start=start.isoformat(), end=end.isoformat(),
                                basis="consolidated", filing={})
            for item in INCOME_ITEMS:
                hit = _pick(cf, item, start, end, fuzzy=q == 1)
                if hit and QUARTER_DAYS[0] <= hit[1].days <= QUARTER_DAYS[1]:
                    p.items[item] = hit[1].val
                elif q > 1 and prev_end:
                    p.items[item] = _diff(cf, item, (fy.start, end), (fy.start, prev_end))
                    p.derived.append(item)
                else:
                    p.items[item] = None
            for item in CASH_ITEMS:
                if q == 1:
                    hit = _pick(cf, item, fy.start, end, fuzzy=True)
                    p.items[item] = hit[1].val if hit else None
                elif prev_end:
                    p.items[item] = _diff(cf, item, (fy.start, end), (fy.start, prev_end))
                    p.derived.append(item)
            first = fy.annual if q == 4 else _first_report(cf, fy.start, end) or _first_report(cf, start, end)
            p.filing = _filing(first)
            _stocks(cf, p, end, first.accn if first else None)
            out.append(_finish(p))
    return out


def raw_facts(doc: dict, p: FinancialPeriod) -> list[dict[str, Any]]:
    """그 기간의 us-gaap 사실 전부 — 기간 말 시점 값과, 종료일이 같은 기간 값(3개월·누적·연간)."""
    end = p.end
    out = []
    for tag, body in ((doc.get("facts") or {}).get("us-gaap") or {}).items():
        for unit, facts in (body.get("units") or {}).items():
            latest: dict[str | None, dict] = {}
            for x in facts:
                if x.get("end") != end or not str(x.get("form") or "").startswith(("10-K", "10-Q")):
                    continue
                k = x.get("start")
                if k not in latest or str(x.get("filed")) > str(latest[k].get("filed")):
                    latest[k] = x
            for x in latest.values():
                out.append({"concept": tag, "unit": unit, "start": x.get("start"), "end": x["end"], "value": x["val"]})
    return out
