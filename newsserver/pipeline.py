"""수집 파이프라인 — 정규화 → 중복 제거 → 태깅 → 저장.

같은 기사(정규화 URL 기준)가 여러 피드에서 오면 기사 행은 하나로 두고 ``article_feeds`` 에
피드 소속을 모두 남긴다. 나중에 온 소스가 더 풍부한 정보(요약·발행 시각·종목 태그)를
주면 기존 행을 보강한다.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

import aiosqlite
from loguru import logger

from newsserver.collectors.base import RawItem, SymbolTarget
from newsserver.config import Settings
from newsserver.markets import normalize_symbol
from newsserver.sources import SourceSpec
from newsserver.storage.db import Database
from newsserver.symbols import SymbolDirectory
from newsserver.textutil import clean_text, title_key, url_key
from newsserver.timeutil import sanitize_published, to_iso, utcnow
from newsserver.topics import TopicRules

# 수집 시 확정 태그 중 사전 재태깅 때 다시 계산하는 방법
DERIVED_SYMBOL_METHODS = ("dict", "ref")


@dataclass
class IngestStats:
    n_items: int = 0
    n_new: int = 0
    n_updated: int = 0
    n_skipped: int = 0
    new_ids: list[int] = field(default_factory=list)


class Ingestor:
    def __init__(self, db: Database, directory: SymbolDirectory, topics: TopicRules, settings: Settings):
        self.db = db
        self.directory = directory
        self.topics = topics
        self.settings = settings

    def _symbol_tags(self, spec: SourceSpec, item: RawItem, title: str, summary: str,
                     target: SymbolTarget | None) -> set[tuple[str, str, str]]:
        tags: dict[tuple[str, str], str] = {}
        for tag in item.symbols:
            sym = normalize_symbol(tag.symbol, tag.market)
            if sym:
                tags[(tag.market, sym)] = tag.method
        cik = item.extra.get("cik")
        if cik:
            for key in self.directory.symbols_for_cik(cik):
                tags.setdefault(key, "edgar_cik")
        if target is not None:
            tags.setdefault((target.market, target.symbol), "search")
        # 공시는 제출 법인 태그가 명확하므로 본문 사전 태깅을 하지 않는다
        if self.settings.symbol_dict_tagging and not spec.is_filing:
            for market, symbol, method in self.directory.tag(title, summary):
                tags.setdefault((market, symbol), method)
        return {(m, s, method) for (m, s), method in tags.items()}

    async def ingest(self, spec: SourceSpec, items: list[RawItem],
                     target: SymbolTarget | None = None) -> IngestStats:
        stats = IngestStats(n_items=len(items))
        if not items:
            return stats
        now = utcnow()
        now_iso = to_iso(now)
        retention = spec.retention_days or self.settings.default_retention_days
        oldest = now - dt.timedelta(days=retention)

        async with self.db.write() as conn:
            for item in items:
                title = clean_text(item.title)
                summary = clean_text(item.summary)
                key = url_key(item.url)
                if not title or not key:
                    stats.n_skipped += 1
                    continue
                published = sanitize_published(item.published_at, now)
                # 보관 기간보다 오래된 항목은 받지 않는다 — 받으면 정리 작업이 지우고 다음 수집이
                # 다시 넣는 일이 반복되어 증분 커서 소비자에게 같은 기사가 계속 새로 보인다
                if published is not None and published < oldest:
                    stats.n_skipped += 1
                    continue

                symbol_tags = self._symbol_tags(spec, item, title, summary, target)
                topic_keys = self.topics.extract(title, summary)

                cur = await conn.execute(
                    "SELECT id, summary, published_at FROM articles WHERE url_key = ?", (key,)
                )
                row = await cur.fetchone()
                if row is None:
                    article_id = await self._insert(conn, spec, item, key, title, summary, published, now_iso)
                    stats.n_new += 1
                    stats.new_ids.append(article_id)
                else:
                    article_id = row["id"]
                    if await self._enrich(conn, row, spec, summary, published):
                        stats.n_updated += 1

                await conn.execute(
                    "INSERT OR IGNORE INTO article_feeds(article_id, source_key, seen_at) VALUES (?, ?, ?)",
                    (article_id, spec.key, now_iso),
                )
                if symbol_tags:
                    await conn.executemany(
                        "INSERT OR IGNORE INTO article_symbols(article_id, market, symbol, method) VALUES (?, ?, ?, ?)",
                        [(article_id, m, s, method) for m, s, method in symbol_tags],
                    )
                if topic_keys:
                    await conn.executemany(
                        "INSERT OR IGNORE INTO article_topics(article_id, topic) VALUES (?, ?)",
                        [(article_id, t) for t in topic_keys],
                    )
        return stats

    async def _insert(self, conn: aiosqlite.Connection, spec: SourceSpec, item: RawItem, key: str,
                      title: str, summary: str, published: dt.datetime | None, now_iso: str) -> int:
        body_kind = spec.body_kind if summary or spec.body_kind == "title_only" else "title_only"
        published_iso = to_iso(published)
        cur = await conn.execute(
            "INSERT INTO articles(url, url_key, title, summary, body_kind, source_key, market, lang, category, "
            "is_filing, published_at, collected_at, ts, title_key, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.url.strip(), key, title, summary, body_kind, spec.key, spec.market, spec.lang,
                spec.category, int(spec.is_filing), published_iso, now_iso, published_iso or now_iso,
                title_key(title), json.dumps(item.extra, ensure_ascii=False, default=str),
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    async def _enrich(conn: aiosqlite.Connection, row, spec: SourceSpec, summary: str,
                      published: dt.datetime | None) -> bool:
        sets: list[str] = []
        params: list = []
        if summary and not (row["summary"] or "").strip():
            sets += ["summary = ?", "body_kind = ?"]
            params += [summary, spec.body_kind if spec.body_kind != "title_only" else "summary"]
        if published is not None and row["published_at"] is None:
            iso = to_iso(published)
            sets += ["published_at = ?", "ts = ?"]
            params += [iso, iso]
        if not sets:
            return False
        await conn.execute(f"UPDATE articles SET {', '.join(sets)} WHERE id = ?", (*params, row["id"]))
        return True

    # ── 재태깅 ──────────────────────────────────────────────────────────────
    async def retag(self, *, topics: bool, symbols: bool, batch: int = 2000) -> int:
        """보관 중인 기사 전체의 주제·사전 종목 태그를 다시 계산한다.

        쓰기 락을 오래 잡지 않도록 배치 단위로 나눠 커밋한다.
        """
        if not (topics or symbols):
            return 0
        last_id = 0
        total = 0
        while True:
            async with self.db.read() as conn:
                cur = await conn.execute(
                    "SELECT id, title, summary, is_filing FROM articles WHERE id > ? ORDER BY id LIMIT ?",
                    (last_id, batch),
                )
                rows = await cur.fetchall()
            if not rows:
                break
            first, last_id = rows[0]["id"], rows[-1]["id"]
            topic_rows: list[tuple[int, str]] = []
            symbol_rows: list[tuple[int, str, str, str]] = []
            for r in rows:
                if topics:
                    topic_rows += [(r["id"], t) for t in self.topics.extract(r["title"], r["summary"])]
                if symbols and not r["is_filing"]:
                    symbol_rows += [(r["id"], m, s, meth) for m, s, meth in self.directory.tag(r["title"], r["summary"])]
            async with self.db.write() as conn:
                if topics:
                    await conn.execute("DELETE FROM article_topics WHERE article_id BETWEEN ? AND ?", (first, last_id))
                    await conn.executemany("INSERT OR IGNORE INTO article_topics(article_id, topic) VALUES (?, ?)", topic_rows)
                if symbols:
                    await conn.execute(
                        f"DELETE FROM article_symbols WHERE article_id BETWEEN ? AND ? "
                        f"AND method IN ({','.join('?' * len(DERIVED_SYMBOL_METHODS))})",
                        (first, last_id, *DERIVED_SYMBOL_METHODS),
                    )
                    await conn.executemany(
                        "INSERT OR IGNORE INTO article_symbols(article_id, market, symbol, method) VALUES (?, ?, ?, ?)",
                        symbol_rows,
                    )
            total += len(rows)
        logger.info("재태깅 완료 — {}건 (topics={}, symbols={})", total, topics, symbols)
        return total
