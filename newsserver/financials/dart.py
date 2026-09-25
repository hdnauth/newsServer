"""한국 — OpenDART 단일회사 전체 재무제표(``fnlttSinglAcntAll``)와 주식총수 현황(``stockTotqySttus``).

보고서 하나가 한 번의 호출이다: (고유번호, 사업연도, 보고서 코드, 연결/별도).

분기 값 계산 (DART 분기·반기 보고서의 표기 방식에 맞춘다)
  손익      보고서의 당기(3개월) 값. 4분기 = 사업보고서(12개월) − 3분기 보고서 누적
  현금흐름  보고서 값이 누적이라 이전 보고서 누적을 뺀다 (2~4분기는 계산값)
  재무상태  보고서 기준일 잔액. 4분기 = 사업보고서
차감에 쓰는 보고서들이 연결/별도 기준이 다르면 계산하지 않는다(None).

계정은 표준계정 ID 로 찾고, 표준계정을 쓰지 않은 회사는 계정명으로 찾는다.
손익계산서가 없는 회사(포괄손익계산서 단일 표시)는 포괄손익계산서에서 찾는다.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

import httpx

from newsserver.config import Settings
from newsserver.financials.cache import MemoryCache
from newsserver.financials.model import (
    ITEM_KIND, FinancialPeriod, NotConfigured, UpstreamError, add_free_cash_flow,
)

ALL_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
SHARES_URL = "https://opendart.fss.or.kr/api/stockTotqySttus.json"

# 분기 → 보고서 코드 (4 = 사업보고서)
REPORT_CODE = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}
REPORT_FORM = {"11013": "1분기보고서", "11012": "반기보고서", "11014": "3분기보고서", "11011": "사업보고서"}
DEFAULT_END = {"11013": (3, 31), "11012": (6, 30), "11014": (9, 30), "11011": (12, 31)}
FS_DIV = {"consolidated": "CFS", "separate": "OFS"}
BASIS = {"CFS": "consolidated", "OFS": "separate"}

INCOME, BALANCE, CASH = ("IS", "CIS"), ("BS",), ("CF",)

# 항목 → (재무제표, 표준계정 ID 후보, 계정명 정규식)
MAPPING: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "revenue": (INCOME, ("ifrs-full_Revenue",), r"매출액|수익\(매출액\)|영업수익|매출"),
    "gross_profit": (INCOME, ("ifrs-full_GrossProfit",), r"매출총이익"),
    "operating_income": (INCOME, ("dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"),
                         r"영업이익"),
    "pretax_income": (INCOME, ("ifrs-full_ProfitLossBeforeTax",), r"법인세비용차감전.*"),
    "net_income": (INCOME, ("ifrs-full_ProfitLoss",), r"(당기|분기|반기)순이익"),
    "net_income_attributable": (INCOME, ("ifrs-full_ProfitLossAttributableToOwnersOfParent",), r"지배기업.*순이익"),
    "eps_basic": (INCOME, ("ifrs-full_BasicEarningsLossPerShare",), r"기본주당(순)?이익"),
    "eps_diluted": (INCOME, ("ifrs-full_DilutedEarningsLossPerShare",), r"희석주당(순)?이익"),
    "total_assets": (BALANCE, ("ifrs-full_Assets",), r"자산총계"),
    "current_assets": (BALANCE, ("ifrs-full_CurrentAssets",), r"유동자산"),
    "cash_and_equivalents": (BALANCE, ("ifrs-full_CashAndCashEquivalents",), r"현금및현금성자산"),
    "total_liabilities": (BALANCE, ("ifrs-full_Liabilities",), r"부채총계"),
    "current_liabilities": (BALANCE, ("ifrs-full_CurrentLiabilities",), r"유동부채"),
    "total_equity": (BALANCE, ("ifrs-full_Equity",), r"자본총계"),
    "equity_attributable": (BALANCE, ("ifrs-full_EquityAttributableToOwnersOfParent",), r"지배기업.*자본"),
    "operating_cash_flow": (CASH, ("ifrs-full_CashFlowsFromUsedInOperatingActivities",),
                            r"영업활동(으로인한)?현금흐름"),
    "capex": (CASH, ("ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                     "ifrs-full_PurchaseOfPropertyPlantAndEquipment"), r"유형자산의?취득"),
    "dividends_paid": (CASH, ("ifrs-full_DividendsPaidClassifiedAsFinancingActivities", "ifrs-full_DividendsPaid"),
                       r"배당금의?지급"),
}
# 현금 유출을 양수로 표기하는 항목
OUTFLOW_ITEMS = {"capex", "dividends_paid"}
_PATTERNS = {k: re.compile(v[2]) for k, v in MAPPING.items()}


def _amount(raw: Any) -> int | float | None:
    s = str(raw if raw is not None else "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        value: int | float = int(s)
    except ValueError:
        try:
            value = float(s)
        except ValueError:
            return None
    return -value if negative else value


def _norm_name(name: str) -> str:
    return re.sub(r"\s+|\((손실|이익)\)", "", name or "")


@dataclass
class Report:
    """보고서 하나(연결 또는 별도)의 계정 행."""
    year: int
    code: str
    fs_div: str
    rcept_no: str
    currency: str
    rows: list[dict[str, Any]]

    def find(self, item: str) -> dict[str, Any] | None:
        statements, ids, _ = MAPPING[item]
        for sj in statements:
            rows = [r for r in self.rows if r["sj_div"] == sj]
            for account_id in ids:
                for r in rows:
                    if r["account_id"] == account_id:
                        return r
            for r in rows:
                if _PATTERNS[item].fullmatch(r["name_norm"]):
                    return r
        return None

    def value(self, item: str, cumulative: bool = False) -> int | float | None:
        row = self.find(item)
        if row is None:
            return None
        if cumulative:
            return row["add"] if row["add"] is not None else row["amount"]
        return row["amount"] if row["amount"] is not None else row["add"]


class DartFinancials:
    source = "dart"

    def __init__(self, settings: Settings, http: httpx.AsyncClient, cache: MemoryCache):
        self.settings = settings
        self.http = http
        self.cache = cache
        self._sem = asyncio.Semaphore(4)

    def _check(self) -> None:
        if not self.settings.dart_api_key:
            raise NotConfigured("DART_API_KEY 미설정")

    async def _call(self, url: str, params: dict) -> dict:
        async with self._sem:
            try:
                resp = await self.http.get(url, params={"crtfc_key": self.settings.dart_api_key, **params})
            except httpx.HTTPError as e:
                raise UpstreamError(f"DART 요청 실패: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise UpstreamError(f"DART HTTP {resp.status_code}")
        data = resp.json()
        status = str(data.get("status"))
        if status not in ("000", "013"):
            raise UpstreamError(f"DART {status}: {data.get('message', '')}")
        return data

    async def report(self, corp_code: str, year: int, code: str, fs_div: str) -> Report | None:
        async def load() -> Report | None:
            data = await self._call(ALL_URL, {"corp_code": corp_code, "bsns_year": str(year),
                                              "reprt_code": code, "fs_div": fs_div})
            rows = data.get("list") or []
            if data.get("status") == "013" or not rows:
                return None
            return Report(
                year=year, code=code, fs_div=fs_div, rcept_no=str(rows[0].get("rcept_no") or ""),
                currency=str(rows[0].get("currency") or "KRW"),
                rows=[{"sj_div": r.get("sj_div", ""), "account_id": r.get("account_id", ""),
                       "account_nm": r.get("account_nm", ""), "name_norm": _norm_name(r.get("account_nm", "")),
                       "amount": _amount(r.get("thstrm_amount")), "add": _amount(r.get("thstrm_add_amount"))}
                      for r in rows],
            )
        return await self.cache.get_or_load(("dart", "report", corp_code, year, code, fs_div), load)

    async def report_with_fallback(self, corp_code: str, year: int, code: str, basis: str) -> Report | None:
        """연결 기준을 요청하면 연결이 없는 회사(종속회사 없음)는 별도 재무제표를 쓴다."""
        rep = await self.report(corp_code, year, code, FS_DIV[basis])
        if rep is None and basis == "consolidated":
            rep = await self.report(corp_code, year, code, "OFS")
        return rep

    async def shares(self, corp_code: str, year: int, code: str) -> dict[str, Any] | None:
        async def load() -> dict[str, Any] | None:
            data = await self._call(SHARES_URL, {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": code})
            for row in data.get("list") or []:
                if str(row.get("se", "")).strip() == "보통주":
                    return {"end": str(row.get("stlm_dt") or "")[:10] or None,
                            "outstanding": _amount(row.get("distb_stock_co"))}
            return None
        return await self.cache.get_or_load(("dart", "shares", corp_code, year, code), load)

    # ── 기간 조립 ────────────────────────────────────────────────────────────
    async def periods(self, corp_code: str, *, period: str, limit: int, basis: str, raw: bool,
                      today: dt.date | None = None) -> tuple[list[FinancialPeriod], str | None]:
        self._check()
        today = today or dt.date.today()
        # 기간이 끝나지 않은 보고서는 있을 수 없으므로 조회하지 않는다 (결산월이 다르면 실제 종료일은 더 늦다)
        if period == "annual":
            years = [y for y in range(today.year, today.year - limit - 1, -1)
                     if dt.date(y, *DEFAULT_END["11011"]) <= today]
            reports = await asyncio.gather(*(self.report_with_fallback(corp_code, y, "11011", basis) for y in years))
            built = [self._annual(r, raw) for r in reports if r is not None][:limit]
        else:
            n_years = limit // 4 + 2
            keys = [(y, q) for y in range(today.year, today.year - n_years, -1) for q in (4, 3, 2, 1)
                    if dt.date(y, *DEFAULT_END[REPORT_CODE[q]]) <= today]
            fetched = await asyncio.gather(*(self.report_with_fallback(corp_code, y, REPORT_CODE[q], basis)
                                             for y, q in keys))
            by_key = dict(zip(keys, fetched))
            built = []
            for y, q in keys:
                p = self._quarter(by_key, y, q, raw)
                if p is not None:
                    built.append(p)
                if len(built) >= limit:
                    break
        await self._apply_shares(corp_code, built)
        currency = next((p.filing.get("currency") for p in built), None)
        for p in built:
            p.filing.pop("currency", None)
        return built, currency

    def _base(self, rep: Report, year: int, quarter: int | None, code: str) -> FinancialPeriod:
        end = dt.date(year, *DEFAULT_END[code])
        return FinancialPeriod(
            fiscal_year=year, fiscal_quarter=quarter, start=_period_start(end, quarter is not None).isoformat(),
            end=end.isoformat(),
            basis=BASIS[rep.fs_div],
            filing={"id": rep.rcept_no, "form": REPORT_FORM[rep.code],
                    "filed": _rcept_date(rep.rcept_no), "currency": rep.currency},
        )

    def _annual(self, rep: Report, raw: bool) -> FinancialPeriod:
        p = self._base(rep, rep.year, None, "11011")
        for item in MAPPING:
            p.items[item] = _sign(item, rep.value(item))
        add_free_cash_flow(p)
        if raw:
            p.raw = _raw(rep)
        return p

    def _quarter(self, by_key: dict, year: int, q: int, raw: bool) -> FinancialPeriod | None:
        rep = by_key.get((year, q))
        if rep is None:
            return None
        prev = by_key.get((year, q - 1)) if q > 1 else None
        if prev is not None and prev.fs_div != rep.fs_div:
            prev = None
        p = self._base(rep, year, q, REPORT_CODE[q])
        for item in MAPPING:
            kind = ITEM_KIND[item]
            value: int | float | None
            if kind == "stock":
                value = rep.value(item)
            elif MAPPING[item][0] == CASH:
                # 누적 값 차감
                cur = rep.value(item, cumulative=True)
                if q == 1:
                    value = cur
                else:
                    before = prev.value(item, cumulative=True) if prev is not None else None
                    value = cur - before if cur is not None and before is not None else None
                    p.derived.append(item)
            elif q == 4:
                total = rep.value(item)
                before = prev.value(item, cumulative=True) if prev is not None else None
                value = total - before if total is not None and before is not None else None
                p.derived.append(item)
            else:
                value = rep.value(item)
            p.items[item] = _sign(item, value)
        p.derived = [k for k in p.derived if p.items.get(k) is not None]
        add_free_cash_flow(p)
        if raw:
            p.raw = _raw(rep)
        return p

    async def _apply_shares(self, corp_code: str, periods: list[FinancialPeriod]) -> None:
        async def one(p: FinancialPeriod) -> None:
            code = REPORT_CODE[p.fiscal_quarter or 4]
            info = await self.shares(corp_code, p.fiscal_year, code)
            if info:
                p.items["shares_outstanding"] = info["outstanding"]
                if info["end"] and info["end"] != p.end:
                    # 결산월이 12월이 아닌 회사 — 보고서 기준일로 기간을 맞춘다
                    end = dt.date.fromisoformat(info["end"])
                    p.end, p.start = end.isoformat(), _period_start(end, p.fiscal_quarter is not None).isoformat()
        await asyncio.gather(*(one(p) for p in periods))


def _period_start(end: dt.date, quarter: bool) -> dt.date:
    """월말 결산 기간의 시작일 — 분기는 3개월, 연간은 12개월."""
    months = 3 if quarter else 12
    idx = end.year * 12 + end.month - months  # 시작 월의 0-기준 인덱스
    return dt.date(idx // 12, idx % 12 + 1, 1)


def _sign(item: str, value: int | float | None) -> int | float | None:
    if value is None:
        return None
    return abs(value) if item in OUTFLOW_ITEMS else value


def _rcept_date(rcept_no: str) -> str | None:
    if len(rcept_no) >= 8 and rcept_no[:8].isdigit():
        return f"{rcept_no[:4]}-{rcept_no[4:6]}-{rcept_no[6:8]}"
    return None


def _raw(rep: Report) -> list[dict[str, Any]]:
    """보고서 원본 계정 — 값은 보고서 표기 그대로(분기 보고서의 현금흐름은 누적)."""
    return [{"statement": r["sj_div"], "account_id": r["account_id"], "account_name": r["account_nm"],
             "amount": r["amount"], "cumulative_amount": r["add"]} for r in rep.rows]
