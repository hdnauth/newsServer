"""API 응답·요청 모델."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceInfo(BaseModel):
    key: str
    label: str
    kind: str | None = None
    market: str | None = None
    lang: str | None = None
    category: str | None = None
    body_kind: str | None = None
    is_filing: bool | None = None


class SymbolRef(BaseModel):
    market: str
    symbol: str
    method: str


class Headline(BaseModel):
    id: int
    title: str
    url: str
    summary: str
    body_kind: Literal["summary", "title_only", "metadata"] | str
    source: SourceInfo
    feeds: list[str]
    market: str
    lang: str
    category: str
    is_filing: bool
    published_at: str | None
    collected_at: str
    ts: str = Field(description="정렬·시간창 기준 시각 = published_at, 없으면 collected_at")
    symbols: list[SymbolRef]
    topics: list[str]
    extra: dict[str, Any] = Field(default_factory=dict)
    match: Literal["tag", "text"] | None = Field(None, description="종목 조회 시 매칭 경로")


class HeadlineList(BaseModel):
    items: list[Headline]
    next_since_id: int | None
    query: dict[str, Any] = Field(default_factory=dict)
    refresh: dict[str, str] | None = None


class SymbolQuery(BaseModel):
    symbol: str
    market: str
    names: list[str] = Field(default_factory=list, description="회사명·별칭 (사전에 없거나 보강할 때)")


class BySymbolsRequest(BaseModel):
    symbols: list[SymbolQuery] = Field(..., max_length=200)
    hours: float | None = Field(None, gt=0)
    limit_per_symbol: int = Field(10, ge=1, le=200)
    match: Literal["any", "tag", "text"] = "any"
    match_summary: bool = True
    filings: Literal["auto", "include", "exclude"] = "auto"
    kind: Literal["all", "news", "filing"] = "all"
    time_basis: Literal["ts", "collected"] = "ts"
    dedup: Literal["none", "title"] = "none"


class BySymbolsResponse(BaseModel):
    results: dict[str, list[Headline]]


class SourceStatus(BaseModel):
    key: str
    kind: str
    label: str
    market: str
    lang: str
    category: str
    body_kind: str
    is_filing: bool
    per_symbol: bool
    enabled: bool
    enabled_override: bool | None
    configured: bool
    config_issue: str | None
    last_fetch_at: str | None
    last_success_at: str | None
    last_new_at: str | None
    last_error: str | None
    fail_streak: int
    next_poll_at: str | None
    articles_24h: int
    articles_total: int


class SourcePatch(BaseModel):
    enabled: bool | None = Field(..., description="true/false, null 이면 sources.yaml 기본값으로 되돌림")


class TopicInfo(BaseModel):
    key: str
    label: str


class TopicList(BaseModel):
    version: int
    topics: list[TopicInfo]


class WatchItem(BaseModel):
    symbol: str
    market: str
    name: str = ""
    aliases: list[str] = Field(default_factory=list)


class Watchlist(BaseModel):
    client: str
    symbols: list[WatchItem] = Field(..., max_length=2000)


class WatchlistPut(BaseModel):
    symbols: list[WatchItem] = Field(..., max_length=2000)


class SymbolInfo(BaseModel):
    market: str
    symbol: str
    name: str
    name_en: str
    aliases: list[str]
    cik: str | None
    origin: str


class AliasesPut(BaseModel):
    aliases: list[str]
    name: str | None = Field(None, description="사전에 없는 종목을 새로 등록할 때 필요")
