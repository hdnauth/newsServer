"""재무제표 — 저장하지 않고 원천(DART·SEC)에서 읽어 정규화해 돌려준다."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from newsserver.api.deps import parse_market, services
from newsserver.financials.model import ITEMS, FinancialsError
from newsserver.markets import normalize_symbol

router = APIRouter(tags=["financials"])


@router.get("/financials/items")
async def financial_items() -> dict:
    """정규화 항목 정의 — 키, 이름, 재무제표, 종류(flow·stock·per_share·shares), 설명."""
    return {"items": [{"key": i.key, "label": i.label, "statement": i.statement, "kind": i.kind,
                       "description": i.description} for i in ITEMS]}


@router.get("/financials/{market}/{symbol}")
async def get_financials(
    market: str, symbol: str,
    period: Literal["quarter", "annual"] = Query("quarter", description="quarter: 3개월 단위, annual: 회계연도"),
    limit: int = Query(8, ge=1, le=20, description="최근 기간 수"),
    basis: Literal["consolidated", "separate"] = Query(
        "consolidated", description="KR 전용. consolidated 는 연결이 없으면 별도로 대체한다"),
    raw: bool = Query(False, description="원천 계정 전부를 함께 돌려준다"),
    svc=Depends(services),
) -> dict:
    mkt = parse_market(market, required=True)
    if mkt == "GLOBAL":
        raise HTTPException(422, "market 은 KR 또는 US 여야 합니다")
    if mkt == "US" and basis == "separate":
        raise HTTPException(422, "US 는 연결 기준만 있습니다")
    try:
        return await svc.financials.get(mkt, normalize_symbol(symbol, mkt), period=period, limit=limit,
                                        basis=basis, raw=raw)
    except FinancialsError as e:
        raise HTTPException(e.status, str(e)) from e
