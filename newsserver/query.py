"""헤드라인 조회.

종목 조회는 두 경로의 합집합이다.
  A. 태그 경로 — 수집 시 확정된 ``article_symbols`` (소스 태그·공시 종목코드·사전 태깅 등)
  B. 텍스트 경로 — 제목/요약을 FTS·LIKE 로 후보 추린 뒤 ``matching`` 규칙으로 정밀 검증
결과 항목의 ``match`` 가 어느 경로로 찾았는지 알려 준다(tag | text).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from newsserver.markets import normalize_symbol
from newsserver.matching import build_search_terms, text_matches_symbol, ticker_needs_exact_case
from newsserver.sources import SourceSpec
from newsserver.storage.db import Database
from newsserver.symbols import SymbolDirectory
from newsserver.timeutil import to_iso, utcnow
from newsserver.topics import TopicRules

# 텍스트 경로에서 정밀 검증할 후보 상한 (최신순)
TEXT_CANDIDATE_CAP = 1000
TAG_CANDIDATE_CAP = 1000
FTS_MIN_LEN = 3  # trigram 토크나이저는 3자 미만 키워드를 찾지 못한다


class QueryError(ValueError):
    pass


@dataclass
class HeadlineQuery:
    symbol: str | None = None
    market: str | None = None
    names: list[str] = field(default_factory=list)
    hours: float | None = None
    since: dt.datetime | None = None
    until: dt.datetime | None = None
    since_id: int | None = None
    limit: int = 50
    topics: list[str] = field(default_factory=list)
    topics_mode: Literal["any", "all"] = "any"
    sources: list[str] = field(default_factory=list)
    exclude_sources: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    lang: str | None = None
    kind: Literal["all", "news", "filing"] = "all"
    body: Literal["any", "with_summary"] = "any"
    q: str | None = None
    match: Literal["any", "tag", "text"] = "any"
    match_summary: bool = True
    filings: Literal["auto", "include", "exclude"] = "auto"
    market_filter: bool = True
    time_basis: Literal["ts", "collected"] = "ts"
    dedup: Literal["none", "title"] = "none"


@dataclass
class HeadlineResult:
    items: list[dict[str, Any]]
    next_since_id: int | None
    terms: list[str] = field(default_factory=list)


def _fts_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class _Where:
    def __init__(self) -> None:
        self.clauses: list[str] = []
        self.params: list[Any] = []

    def add(self, clause: str, *params: Any) -> None:
        self.clauses.append(clause)
        self.params.extend(params)

    def sql(self) -> str:
        return " AND ".join(self.clauses) if self.clauses else "1=1"


def _text_clause(terms: list[str], *, include_summary: bool) -> tuple[str, list[Any]]:
    """키워드 목록 → (FTS OR LIKE) 조건."""
    parts: list[str] = []
    params: list[Any] = []
    fts_terms = [t for t in terms if len(t) >= FTS_MIN_LEN]
    if fts_terms:
        if include_summary:
            expr = " OR ".join(_fts_phrase(t) for t in fts_terms)
        else:
            expr = " OR ".join(f"title : {_fts_phrase(t)}" for t in fts_terms)
        parts.append("a.id IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)")
        params.append(expr)
    for t in terms:
        if len(t) >= FTS_MIN_LEN:
            continue
        parts.append("a.title LIKE ? ESCAPE '\\'")
        params.append(_like(t))
        if include_summary:
            parts.append("a.summary LIKE ? ESCAPE '\\'")
            params.append(_like(t))
    return ("(" + " OR ".join(parts) + ")" if parts else "0"), params


class QueryService:
    def __init__(self, db: Database, directory: SymbolDirectory, topics: TopicRules,
                 specs: dict[str, SourceSpec]):
        self.db = db
        self.directory = directory
        self.topics = topics
        self.specs = specs
        self._topic_rank = {t.key: i for i, t in enumerate(topics.topics)}

    # ── 공통 조건 ────────────────────────────────────────────────────────────
    def _base_where(self, q: HeadlineQuery, *, for_symbol: bool) -> _Where:
        w = _Where()
        col = "a.collected_at" if q.time_basis == "collected" else "a.ts"
        now = utcnow()
        if q.hours is not None:
            w.add(f"{col} >= ?", to_iso(now - dt.timedelta(hours=q.hours)))
        if q.since is not None:
            w.add(f"{col} >= ?", to_iso(q.since))
        if q.until is not None:
            w.add(f"{col} < ?", to_iso(q.until))
        if q.since_id is not None:
            w.add("a.id > ?", q.since_id)
        if q.sources:
            w.add(f"a.id IN (SELECT article_id FROM article_feeds WHERE source_key IN ({_qs(q.sources)}))",
                  *q.sources)
        if q.exclude_sources:
            # 제외 피드에'만' 실린 기사를 뺀다 — 다른 피드에도 실렸다면 남긴다
            w.add(f"EXISTS (SELECT 1 FROM article_feeds f WHERE f.article_id = a.id "
                  f"AND f.source_key NOT IN ({_qs(q.exclude_sources)}))", *q.exclude_sources)
        if q.categories:
            w.add(f"a.category IN ({_qs(q.categories)})", *q.categories)
        if q.lang:
            w.add("a.lang = ?", q.lang)
        if q.kind == "news":
            w.add("a.is_filing = 0")
        elif q.kind == "filing":
            w.add("a.is_filing = 1")
        if q.body == "with_summary":
            w.add("a.summary != ''")
        if q.topics:
            if q.topics_mode == "all":
                for t in q.topics:
                    w.add("EXISTS (SELECT 1 FROM article_topics t WHERE t.article_id = a.id AND t.topic = ?)", t)
            else:
                w.add(f"a.id IN (SELECT article_id FROM article_topics WHERE topic IN ({_qs(q.topics)}))",
                      *q.topics)
        if q.q:
            clause, params = _text_clause([q.q.strip()], include_summary=True)
            w.add(clause, *params)
        if q.market and not for_symbol:
            w.add("a.market = ?", q.market)
        return w

    def _order(self, q: HeadlineQuery) -> str:
        if q.since_id is not None:
            return "a.id ASC"
        return "a.collected_at DESC, a.id DESC" if q.time_basis == "collected" else "a.ts DESC, a.id DESC"

    # ── 조회 ────────────────────────────────────────────────────────────────
    async def headlines(self, q: HeadlineQuery) -> HeadlineResult:
        if q.symbol:
            rows, match, terms = await self._symbol_rows(q)
        else:
            rows, match, terms = await self._plain_rows(q), {}, []

        if q.dedup == "title":
            seen: set[str] = set()
            deduped = []
            for r in rows:
                if r["title_key"] in seen:
                    continue
                seen.add(r["title_key"])
                deduped.append(r)
            rows = deduped
        rows = rows[: q.limit]

        items = await self._hydrate(rows)
        for item in items:
            if item["id"] in match:
                item["match"] = match[item["id"]]
        if q.since_id is not None:
            next_id = max([q.since_id, *(i["id"] for i in items)])
        else:
            next_id = max((i["id"] for i in items), default=None)
        return HeadlineResult(items=items, next_since_id=next_id, terms=terms)

    async def _plain_rows(self, q: HeadlineQuery) -> list:
        w = self._base_where(q, for_symbol=False)
        fetch = q.limit * 3 if q.dedup == "title" else q.limit
        async with self.db.read() as conn:
            cur = await conn.execute(
                f"SELECT a.* FROM articles a WHERE {w.sql()} ORDER BY {self._order(q)} LIMIT ?",
                (*w.params, fetch),
            )
            return list(await cur.fetchall())

    async def _symbol_rows(self, q: HeadlineQuery) -> tuple[list, dict[int, str], list[str]]:
        if not q.market or q.market == "GLOBAL":
            raise QueryError("symbol 조회에는 market(KR|US)이 필요합니다")
        symbol = normalize_symbol(q.symbol or "", q.market)
        names = list(dict.fromkeys([*q.names, *self.directory.names_for(q.market, symbol)]))
        terms = build_search_terms(symbol, q.market, names)
        exact_case = ticker_needs_exact_case(symbol, q.market, names)
        order = self._order(q)

        rows: dict[int, Any] = {}
        match: dict[int, str] = {}

        async with self.db.read() as conn:
            if q.match in ("any", "tag"):
                w = self._base_where(q, for_symbol=True)
                if q.filings == "exclude":
                    w.add("a.is_filing = 0")
                cur = await conn.execute(
                    f"SELECT a.* FROM article_symbols s JOIN articles a ON a.id = s.article_id "
                    f"WHERE s.market = ? AND s.symbol = ? AND {w.sql()} ORDER BY {order} LIMIT ?",
                    (q.market, symbol, *w.params, TAG_CANDIDATE_CAP),
                )
                for r in await cur.fetchall():
                    rows[r["id"]] = r
                    match[r["id"]] = "tag"

            if q.match in ("any", "text") and terms:
                w = self._base_where(q, for_symbol=True)
                clause, params = _text_clause(terms, include_summary=q.match_summary)
                w.add(clause, *params)
                if q.market_filter:
                    w.add("a.market = ?", q.market)
                # 공시 본문은 메타데이터라 텍스트 매칭 근거로 약하다 — 태그로 특정될 때만 쓴다
                if q.filings in ("auto", "exclude"):
                    w.add("a.is_filing = 0")
                cur = await conn.execute(
                    f"SELECT a.* FROM articles a WHERE {w.sql()} ORDER BY {order} LIMIT ?",
                    (*w.params, TEXT_CANDIDATE_CAP),
                )
                for r in await cur.fetchall():
                    if r["id"] in rows:
                        continue
                    haystack = r["title"]
                    if q.match_summary and r["summary"]:
                        haystack = f"{haystack}\n{r['summary']}"
                    if text_matches_symbol(haystack, symbol, terms, exact_case):
                        rows[r["id"]] = r
                        match[r["id"]] = "text"

        ordered = list(rows.values())
        if q.since_id is not None:
            ordered.sort(key=lambda r: r["id"])
        elif q.time_basis == "collected":
            ordered.sort(key=lambda r: (r["collected_at"], r["id"]), reverse=True)
        else:
            ordered.sort(key=lambda r: (r["ts"], r["id"]), reverse=True)
        return ordered, match, terms

    async def article(self, article_id: int) -> dict[str, Any] | None:
        async with self.db.read() as conn:
            cur = await conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,))
            row = await cur.fetchone()
        if row is None:
            return None
        return (await self._hydrate([row]))[0]

    async def _hydrate(self, rows: list) -> list[dict[str, Any]]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        feeds: dict[int, list[str]] = {}
        symbols: dict[int, list[dict]] = {}
        topics: dict[int, list[str]] = {}
        async with self.db.read() as conn:
            ph = _qs(ids)
            cur = await conn.execute(
                f"SELECT article_id, source_key FROM article_feeds WHERE article_id IN ({ph}) ORDER BY seen_at", ids)
            for r in await cur.fetchall():
                feeds.setdefault(r[0], []).append(r[1])
            cur = await conn.execute(
                f"SELECT article_id, market, symbol, method FROM article_symbols WHERE article_id IN ({ph})", ids)
            for r in await cur.fetchall():
                symbols.setdefault(r[0], []).append({"market": r[1], "symbol": r[2], "method": r[3]})
            cur = await conn.execute(
                f"SELECT article_id, topic FROM article_topics WHERE article_id IN ({ph})", ids)
            for r in await cur.fetchall():
                topics.setdefault(r[0], []).append(r[1])

        out = []
        for r in rows:
            spec = self.specs.get(r["source_key"])
            out.append({
                "id": r["id"],
                "title": r["title"],
                "url": r["url"],
                "summary": r["summary"],
                "body_kind": r["body_kind"],
                "source": self.source_info(r["source_key"], spec),
                "feeds": feeds.get(r["id"], [r["source_key"]]),
                "market": r["market"],
                "lang": r["lang"],
                "category": r["category"],
                "is_filing": bool(r["is_filing"]),
                "published_at": r["published_at"],
                "collected_at": r["collected_at"],
                "ts": r["ts"],
                "symbols": symbols.get(r["id"], []),
                "topics": sorted(topics.get(r["id"], []), key=self._topic_order),
                "extra": json.loads(r["extra_json"] or "{}"),
            })
        return out

    def _topic_order(self, key: str) -> tuple[int, str]:
        """topics.yaml 정의 순서, 규칙에서 빠진 옛 주제는 뒤로."""
        return self._topic_rank.get(key, len(self._topic_rank)), key

    @staticmethod
    def source_info(key: str, spec: SourceSpec | None) -> dict[str, Any]:
        if spec is None:
            return {"key": key, "label": key}
        return {"key": key, "label": spec.label, "kind": spec.kind, "market": spec.market,
                "lang": spec.lang, "category": spec.category, "body_kind": spec.body_kind,
                "is_filing": spec.is_filing}

    # ── 주제 통계 ────────────────────────────────────────────────────────────
    async def topic_stats(self, days: float, market: str | None = None) -> dict[str, Any]:
        cutoff = to_iso(utcnow() - dt.timedelta(days=days))
        w = _Where()
        w.add("a.ts >= ?", cutoff)
        if market:
            w.add("a.market = ?", market)
        async with self.db.read() as conn:
            cur = await conn.execute(f"SELECT COUNT(*) FROM articles a WHERE {w.sql()}", w.params)
            total_articles = (await cur.fetchone())[0]
            cur = await conn.execute(
                f"SELECT t.topic, COUNT(*) AS n FROM article_topics t JOIN articles a ON a.id = t.article_id "
                f"WHERE {w.sql()} GROUP BY t.topic ORDER BY n DESC",
                w.params,
            )
            rows = await cur.fetchall()
        total_tags = sum(r["n"] for r in rows)
        return {
            "days": days,
            "market": market,
            "total_articles": total_articles,
            "topics": [
                {
                    "key": r["topic"],
                    "label": self.topics.labels.get(r["topic"], r["topic"]),
                    "count": r["n"],
                    "share_pct": round(r["n"] / total_tags * 100, 1) if total_tags else 0.0,
                    "article_pct": round(r["n"] / total_articles * 100, 1) if total_articles else 0.0,
                }
                for r in rows
            ],
        }


def _qs(values: list) -> str:
    return ",".join("?" * len(values))
