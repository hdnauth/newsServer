"""헤드라인 조회."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from newsserver.api.deps import parse_market, services, split_csv
from newsserver.api.schemas import BySymbolsRequest, BySymbolsResponse, Headline, HeadlineList
from newsserver.markets import normalize_symbol
from newsserver.query import HeadlineQuery, QueryError

router = APIRouter(tags=["headlines"])

MAX_HOURS = 24 * 3660


@router.get("/headlines", response_model=HeadlineList, response_model_exclude_none=False)
async def list_headlines(
    symbol: str | None = Query(None, description="종목 코드/티커. market 필요"),
    market: str | None = Query(None, description="KR | US | GLOBAL (KRX·KOSPI·NASDAQ 등 별칭 허용)"),
    name: list[str] | None = Query(None, description="종목 회사명 (반복 가능)"),
    alias: list[str] | None = Query(None, description="종목 별칭 (반복 가능)"),
    hours: float | None = Query(None, gt=0, le=MAX_HOURS, description="최근 N시간"),
    since: dt.datetime | None = Query(None, description="이 시각 이후 (ISO-8601)"),
    until: dt.datetime | None = Query(None, description="이 시각 이전 (ISO-8601)"),
    since_id: int | None = Query(None, ge=0, description="증분 커서 — 이 id 초과, id 오름차순"),
    limit: int = Query(50, ge=1, le=500),
    topics: list[str] | None = Query(None, description="주제 키 (콤마 구분 또는 반복)"),
    topics_mode: Literal["any", "all"] = "any",
    sources: list[str] | None = Query(None, description="이 피드들에 실린 기사만"),
    exclude_sources: list[str] | None = Query(None, description="이 피드들에만 실린 기사 제외"),
    category: list[str] | None = Query(None),
    lang: str | None = Query(None, description="ko | en"),
    kind: Literal["all", "news", "filing"] = "all",
    body: Literal["any", "with_summary"] = "any",
    q: str | None = Query(None, min_length=2, description="전문 검색 (제목·요약 부분 일치)"),
    match: Literal["any", "tag", "text"] = Query("any", description="종목 매칭 경로"),
    match_summary: bool = Query(True, description="종목 텍스트 매칭에 요약 포함"),
    filings: Literal["auto", "include", "exclude"] = Query(
        "auto", description="auto: 공시는 종목 태그로 특정될 때만 포함"),
    market_filter: bool = Query(True, description="텍스트 매칭을 같은 시장 소스로 제한"),
    time_basis: Literal["ts", "collected"] = "ts",
    dedup: Literal["none", "title"] = "none",
    refresh: bool = Query(False, description="종목별 소스를 즉시 수집한 뒤 조회 (쿨다운 적용)"),
    svc=Depends(services),
) -> dict:
    mkt = parse_market(market, required=bool(symbol))
    names = [n for n in [*(name or []), *(alias or [])] if n.strip()]
    topic_keys = split_csv(topics)
    unknown = [t for t in topic_keys if t not in svc.topics.labels]
    if unknown:
        raise HTTPException(422, f"알 수 없는 주제: {unknown}")

    refresh_outcome = None
    if refresh and symbol:
        refresh_outcome = await svc.scheduler.refresh_symbol(mkt, normalize_symbol(symbol, mkt), names)

    query = HeadlineQuery(
        symbol=symbol, market=mkt, names=names, hours=hours, since=since, until=until, since_id=since_id,
        limit=limit, topics=topic_keys, topics_mode=topics_mode, sources=split_csv(sources),
        exclude_sources=split_csv(exclude_sources), categories=split_csv(category), lang=lang, kind=kind,
        body=body, q=q, match=match, match_summary=match_summary, filings=filings,
        market_filter=market_filter, time_basis=time_basis, dedup=dedup,
    )
    try:
        result = await svc.query.headlines(query)
    except QueryError as e:
        raise HTTPException(422, str(e)) from e

    echo = {k: v for k, v in {
        "symbol": normalize_symbol(symbol, mkt) if symbol else None, "market": mkt, "hours": hours,
        "topics": topic_keys or None, "since_id": since_id, "limit": limit,
    }.items() if v is not None}
    if result.terms:
        echo["terms"] = result.terms
    return {"items": result.items, "next_since_id": result.next_since_id, "query": echo,
            "refresh": refresh_outcome}


@router.post("/headlines/by-symbols", response_model=BySymbolsResponse)
async def headlines_by_symbols(body: BySymbolsRequest, svc=Depends(services)) -> dict:
    results: dict[str, list] = {}
    for item in body.symbols:
        mkt = parse_market(item.market, required=True)
        sym = normalize_symbol(item.symbol, mkt)
        query = HeadlineQuery(
            symbol=sym, market=mkt, names=item.names, hours=body.hours, limit=body.limit_per_symbol,
            match=body.match, match_summary=body.match_summary, filings=body.filings, kind=body.kind,
            time_basis=body.time_basis, dedup=body.dedup,
            sources=list(body.sources), exclude_sources=list(body.exclude_sources),
        )
        try:
            results[f"{mkt}:{sym}"] = (await svc.query.headlines(query)).items
        except QueryError as e:
            raise HTTPException(422, str(e)) from e
    return {"results": results}


@router.get("/articles/{article_id}", response_model=Headline)
async def get_article(article_id: int, svc=Depends(services)) -> dict:
    item = await svc.query.article(article_id)
    if item is None:
        raise HTTPException(404, "기사가 없습니다")
    return item
