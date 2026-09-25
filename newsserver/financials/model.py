"""재무제표 조회의 공통 정의 — 정규화 항목, 기간, 오류.

서버는 재무 수치를 저장하지 않는다. 요청이 오면 원천(DART·SEC)에서 읽어 이 정의대로 정규화해
돌려주고, 같은 원천 응답은 프로세스 메모리에만 잠시 둔다(``cache.py``).

항목 종류
  flow      기간 합계 (손익·현금흐름). 분기는 3개월, 연간은 12개월 값
  stock     기간 말 잔액 (재무상태표)
  per_share 주당 값 (EPS). 분기 4분기는 연간 − 3분기 누적으로 계산한 근사값
  shares    기간 말(또는 그 직후) 주식 수
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ItemKind = Literal["flow", "stock", "per_share", "shares"]


@dataclass(frozen=True)
class ItemDef:
    key: str
    label: str
    statement: Literal["income", "balance", "cash_flow", "shares"]
    kind: ItemKind
    description: str


ITEMS: tuple[ItemDef, ...] = (
    ItemDef("revenue", "매출액", "income", "flow", "영업수익. 금융업처럼 매출 개념이 다른 회사는 없을 수 있다"),
    ItemDef("gross_profit", "매출총이익", "income", "flow", "매출액 − 매출원가"),
    ItemDef("operating_income", "영업이익", "income", "flow", "영업이익(손실)"),
    ItemDef("pretax_income", "법인세비용차감전순이익", "income", "flow", "계속영업 기준 세전 이익"),
    ItemDef("net_income", "당기순이익", "income", "flow", "비지배지분 포함 순이익"),
    ItemDef("net_income_attributable", "지배주주 순이익", "income", "flow", "지배기업 소유주 귀속 순이익 (PER·ROE 계산 기준)"),
    ItemDef("eps_basic", "기본 주당순이익", "income", "per_share", "통화 단위/주"),
    ItemDef("eps_diluted", "희석 주당순이익", "income", "per_share", "통화 단위/주"),
    ItemDef("total_assets", "자산총계", "balance", "stock", ""),
    ItemDef("current_assets", "유동자산", "balance", "stock", ""),
    ItemDef("cash_and_equivalents", "현금및현금성자산", "balance", "stock", ""),
    ItemDef("total_liabilities", "부채총계", "balance", "stock", ""),
    ItemDef("current_liabilities", "유동부채", "balance", "stock", ""),
    ItemDef("total_equity", "자본총계", "balance", "stock", "비지배지분 포함"),
    ItemDef("equity_attributable", "지배주주 지분", "balance", "stock", "지배기업 소유주 귀속 자본 (PBR·ROE 계산 기준)"),
    ItemDef("operating_cash_flow", "영업활동현금흐름", "cash_flow", "flow", ""),
    ItemDef("capex", "유형자산 취득", "cash_flow", "flow", "현금 유출액을 양수로 표기"),
    ItemDef("free_cash_flow", "잉여현금흐름", "cash_flow", "flow", "영업활동현금흐름 − 유형자산 취득 (서버 계산)"),
    ItemDef("dividends_paid", "배당금 지급", "cash_flow", "flow", "현금 유출액을 양수로 표기"),
    ItemDef("shares_outstanding", "유통 보통주식수", "shares", "shares",
            "자기주식을 뺀 보통주. KR 은 반기·사업보고서 기준일 값(1·3분기 보고서는 공시하지 않아 null), "
            "US 는 기간 말 값이 없으면 공시 표지의 기준일 값"),
)
ITEM_KEYS = tuple(i.key for i in ITEMS)
ITEM_KIND = {i.key: i.kind for i in ITEMS}

PeriodType = Literal["quarter", "annual"]


@dataclass
class FinancialPeriod:
    fiscal_year: int
    fiscal_quarter: int | None          # 연간이면 None
    start: str | None                   # YYYY-MM-DD
    end: str
    basis: str                          # consolidated | separate
    filing: dict[str, Any]              # {"id", "form", "filed"}
    items: dict[str, float | int | None] = field(default_factory=dict)
    derived: list[str] = field(default_factory=list)   # 서버가 계산한 항목
    raw: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "fiscal_year": self.fiscal_year, "fiscal_quarter": self.fiscal_quarter,
            "start": self.start, "end": self.end, "basis": self.basis, "filing": self.filing,
            "items": {k: self.items.get(k) for k in ITEM_KEYS}, "derived": sorted(self.derived),
        }
        if self.raw is not None:
            out["raw"] = self.raw
        return out


class FinancialsError(Exception):
    status = 500


class NotConfigured(FinancialsError):
    """원천 자격 증명이 없다 (DART_API_KEY, EDGAR_USER_AGENT)."""
    status = 409


class NotFound(FinancialsError):
    """사전에 없거나 원천 식별자(DART 고유번호·CIK)가 없는 종목."""
    status = 404


class UpstreamError(FinancialsError):
    """원천 오류·한도 초과 — '데이터 없음'과 구분한다."""
    status = 502


def add_free_cash_flow(p: FinancialPeriod) -> None:
    ocf, capex = p.items.get("operating_cash_flow"), p.items.get("capex")
    if ocf is not None and capex is not None:
        p.items["free_cash_flow"] = ocf - capex
        p.derived.append("free_cash_flow")
