"""주제 목록·이름 추론·분포 통계."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from newsserver.api.deps import parse_market, services
from newsserver.api.schemas import TopicList

router = APIRouter(tags=["topics"])


@router.get("/topics", response_model=TopicList)
async def list_topics(svc=Depends(services)) -> dict:
    return {"version": svc.topics.version,
            "topics": [{"key": t.key, "label": t.label} for t in svc.topics.topics]}


@router.get("/topics/for")
async def topics_for(name: str = Query(..., min_length=1), symbol: str = "", svc=Depends(services)) -> dict:
    """종목·상품 이름에서 관련 주제를 추론한다 (예: 지수 추종 상품 → 지수·섹터 주제)."""
    keys = svc.topics.for_name(name, symbol)
    return {"topics": [{"key": k, "label": svc.topics.labels.get(k, k)} for k in keys]}


@router.get("/topics/stats")
async def topic_stats(
    days: float = Query(30, gt=0, le=3660),
    market: str | None = None,
    svc=Depends(services),
) -> dict:
    """기간 내 주제별 기사 수. share_pct 는 태그 전체 중 비중, article_pct 는 기사 중 비중."""
    return await svc.query.topic_stats(days, parse_market(market))
