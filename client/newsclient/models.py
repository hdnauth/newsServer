"""응답 모델 — 서버 JSON 을 파이썬 객체로."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class SymbolRef:
    market: str
    symbol: str
    method: str


@dataclass
class Headline:
    """기사 한 건. 시각은 전부 tz-aware UTC datetime."""

    id: int
    title: str
    url: str
    summary: str
    body_kind: str  # summary | title_only | metadata
    source: str  # 소스 key
    source_label: str
    feeds: list[str]
    market: str
    lang: str
    category: str
    is_filing: bool
    published_at: dt.datetime | None
    collected_at: dt.datetime
    ts: dt.datetime
    symbols: list[SymbolRef] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    match: str | None = None  # 종목 조회 시 tag | text
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_body(self) -> bool:
        return self.body_kind == "summary" and bool(self.summary)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Headline":
        src = d.get("source") or {}
        return cls(
            id=d["id"], title=d["title"], url=d["url"], summary=d.get("summary", ""),
            body_kind=d.get("body_kind", ""), source=src.get("key", ""), source_label=src.get("label", ""),
            feeds=list(d.get("feeds") or []), market=d.get("market", ""), lang=d.get("lang", ""),
            category=d.get("category", ""), is_filing=bool(d.get("is_filing")),
            published_at=_parse_dt(d.get("published_at")), collected_at=_parse_dt(d["collected_at"]),
            ts=_parse_dt(d["ts"]), symbols=[SymbolRef(**s) for s in d.get("symbols") or []],
            topics=list(d.get("topics") or []), extra=dict(d.get("extra") or {}), match=d.get("match"), raw=d,
        )


@dataclass
class HeadlinePage:
    items: list[Headline]
    next_since_id: int | None
    query: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    error: str | None = None
