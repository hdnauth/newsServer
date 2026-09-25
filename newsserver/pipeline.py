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
from newsserver.symbols import SymbolDirectory, TagChanges
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

    def _fixed_symbol_tags(self, item: RawItem, target: SymbolTarget | None) -> dict[tuple[str, str], str]:
        """피드·수집 대상이 정해 주는 종목 태그 (이미 저장된 기사에도 붙인다)."""
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
        return tags

    def _derived_tags(self, title: str, summary: str, is_filing: bool) -> tuple[set[tuple[str, str, str]], list[str]]:
        """저장된 제목·요약에서 계산하는 태그 — (사전 종목 태그, 주제). 재태깅과 같은 규칙이다."""
        # 공시는 제출 법인 태그가 명확하므로 본문 사전 태깅을 하지 않는다
        symbols = (self.directory.tag(title, summary)
                   if self.settings.symbol_dict_tagging and not is_filing else set())
        return symbols, self.topics.extract(title, summary)

    async def _known_keys(self, keys: list[str]) -> set[str]:
        known: set[str] = set()
        async with self.db.read() as conn:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                cur = await conn.execute(
                    f"SELECT url_key FROM articles WHERE url_key IN ({','.join('?' * len(chunk))})", chunk)
                known.update(r[0] for r in await cur.fetchall())
        return known

    async def ingest(self, spec: SourceSpec, items: list[RawItem],
                     target: SymbolTarget | None = None) -> IngestStats:
        stats = IngestStats(n_items=len(items))
        if not items:
            return stats
        now = utcnow()
        now_iso = to_iso(now)
        retention = spec.retention_days or self.settings.default_retention_days
        oldest = now - dt.timedelta(days=retention)

        prepared = []
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
            prepared.append((item, key, title, summary, published))
        if not prepared:
            return stats

        # 피드 항목 대부분은 이미 저장된 기사다. 그 사전·주제 태그는 저장된 제목·요약으로 이미
        # 계산돼 있으므로 신규 항목만 계산하고, 계산은 쓰기 락 밖에서 한다
        known = await self._known_keys([p[1] for p in prepared])
        derived = {key: self._derived_tags(title, summary, spec.is_filing)
                   for _, key, title, summary, _ in prepared if key not in known}

        async with self.db.write() as conn:
            for item, key, title, summary, published in prepared:
                tags = self._fixed_symbol_tags(item, target)
                topic_keys: list[str] = []
                cur = await conn.execute(
                    "SELECT id, title, summary, published_at, is_filing FROM articles WHERE url_key = ?", (key,)
                )
                row = await cur.fetchone()
                if row is None:
                    article_id = await self._insert(conn, spec, item, key, title, summary, published, now_iso)
                    stats.n_new += 1
                    stats.new_ids.append(article_id)
                    # 읽기와 쓰기 사이에 정리 작업이 지운 기사면 여기서 계산한다
                    dict_tags, topic_keys = derived.get(key) or self._derived_tags(title, summary, spec.is_filing)
                else:
                    article_id = row["id"]
                    enriched, new_summary = await self._enrich(conn, row, spec, summary, published)
                    if enriched:
                        stats.n_updated += 1
                    dict_tags = set()
                    if new_summary:
                        # 요약이 새로 채워졌으면 저장된 텍스트가 바뀐 것이다 — 재태깅과 같게 다시 계산한다
                        dict_tags, topic_keys = self._derived_tags(row["title"], new_summary, bool(row["is_filing"]))
                        await conn.execute(
                            f"DELETE FROM article_symbols WHERE article_id = ? "
                            f"AND method IN ({','.join('?' * len(DERIVED_SYMBOL_METHODS))})",
                            (article_id, *DERIVED_SYMBOL_METHODS),
                        )
                for market, symbol, method in dict_tags:
                    tags.setdefault((market, symbol), method)

                await conn.execute(
                    "INSERT OR IGNORE INTO article_feeds(article_id, source_key, seen_at) VALUES (?, ?, ?)",
                    (article_id, spec.key, now_iso),
                )
                if tags:
                    await conn.executemany(
                        "INSERT OR IGNORE INTO article_symbols(article_id, market, symbol, method) VALUES (?, ?, ?, ?)",
                        [(article_id, m, s, method) for (m, s), method in tags.items()],
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
                      published: dt.datetime | None) -> tuple[bool, str | None]:
        """기존 행을 보강한다. 반환: (보강 여부, 새로 채운 요약 — 없으면 None)."""
        sets: list[str] = []
        params: list = []
        new_summary = None
        if summary and not (row["summary"] or "").strip():
            sets += ["summary = ?", "body_kind = ?"]
            params += [summary, spec.body_kind if spec.body_kind != "title_only" else "summary"]
            new_summary = summary
        if published is not None and row["published_at"] is None:
            iso = to_iso(published)
            sets += ["published_at = ?", "ts = ?"]
            params += [iso, iso]
        if not sets:
            return False, None
        await conn.execute(f"UPDATE articles SET {', '.join(sets)} WHERE id = ?", (*params, row["id"]))
        return True, new_summary

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

    async def retag_changed(self, changes: TagChanges, batch: int = 2000) -> int:
        """사전 변경의 영향을 받을 수 있는 기사만 사전 종목 태그를 다시 계산한다.

        사전 태깅 결과는 본문에 나온 이름의 색인 항목으로만 정해지므로, 바뀐 이름(또는 그 이름의
        한 단어)이 제목·요약에 있는 기사와 추가·삭제된 종목의 명시적 참조·기존 태그가 있는 기사만
        보면 된다. 결과는 전체 재태깅과 같다.
        """
        if not changes:
            return 0
        fts_terms, like_terms = _candidate_terms(changes)
        if len(fts_terms) + len(like_terms) > MAX_INCREMENTAL_TERMS:
            logger.info("사전 변경이 커서({}개 이름) 전체 재태깅", len(changes.names))
            return await self.retag(topics=False, symbols=True, batch=batch)

        ids: set[int] = set()
        async with self.db.read() as conn:
            for i in range(0, len(fts_terms), 100):
                expr = " OR ".join('"' + t.replace('"', '""') + '"' for t in fts_terms[i:i + 100])
                cur = await conn.execute("SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?", (expr,))
                ids.update(r[0] for r in await cur.fetchall())
            for i in range(0, len(like_terms), 50):
                chunk = [_like(t) for t in like_terms[i:i + 50]]
                cond = " OR ".join("title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\'" for _ in chunk)
                cur = await conn.execute(f"SELECT id FROM articles WHERE {cond}", [p for t in chunk for p in (t, t)])
                ids.update(r[0] for r in await cur.fetchall())
            for market, symbol in changes.codes:
                cur = await conn.execute(
                    "SELECT article_id FROM article_symbols WHERE market = ? AND symbol = ? "
                    f"AND method IN ({','.join('?' * len(DERIVED_SYMBOL_METHODS))})",
                    (market, symbol, *DERIVED_SYMBOL_METHODS))
                ids.update(r[0] for r in await cur.fetchall())

        ordered = sorted(ids)
        for i in range(0, len(ordered), batch):
            chunk = ordered[i:i + batch]
            ph = ",".join("?" * len(chunk))
            async with self.db.read() as conn:
                cur = await conn.execute(f"SELECT id, title, summary, is_filing FROM articles WHERE id IN ({ph})", chunk)
                rows = await cur.fetchall()
            symbol_rows = [(r["id"], m, s, meth) for r in rows if not r["is_filing"]
                           for m, s, meth in self.directory.tag(r["title"], r["summary"])]
            async with self.db.write() as conn:
                await conn.execute(
                    f"DELETE FROM article_symbols WHERE article_id IN ({ph}) "
                    f"AND method IN ({','.join('?' * len(DERIVED_SYMBOL_METHODS))})",
                    (*chunk, *DERIVED_SYMBOL_METHODS),
                )
                await conn.executemany(
                    "INSERT OR IGNORE INTO article_symbols(article_id, market, symbol, method) VALUES (?, ?, ?, ?)",
                    symbol_rows,
                )
        logger.info("사전 변경 재태깅 — 이름 {}개·종목 {}개 → 기사 {}건", len(changes.names), len(changes.codes), len(ordered))
        return len(ordered)


# 이보다 많은 이름이 바뀌면(첫 적재 등) 후보를 고르는 것보다 전체 재태깅이 싸다
MAX_INCREMENTAL_TERMS = 5000
FTS_MIN_LEN = 3  # trigram 토크나이저는 3자 미만 구절을 찾지 못한다


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _candidate_terms(changes: TagChanges) -> tuple[list[str], list[str]]:
    """재태깅 후보를 찾을 부분 문자열 (FTS 구절, LIKE). 대소문자는 둘 다 무시한다.

    * 한글 이름: 이름 그대로 (토큰 앞부분으로 매칭하므로 본문에 이름이 그대로 있다)
    * 영문 이름: 가장 긴 단어 — 단어 사이 공백 수가 달라도 각 단어는 본문에 그대로 있다
    * 추가·삭제된 종목: ``$NVDA``·``NVDA)`` (명시적 참조의 필수 부분), 한국 종목코드
    """
    terms: set[str] = set()
    for name in changes.names:
        terms.add(max(name.split(" "), key=len) if " " in name else name)
    for market, symbol in changes.codes:
        if market == "KR":
            terms.add(symbol)
        else:
            written = symbol.replace("-", ".")
            terms.update({"$" + written, written + ")"})
    fts = sorted(t for t in terms if len(t) >= FTS_MIN_LEN)
    like = sorted(t for t in terms if 0 < len(t) < FTS_MIN_LEN)
    return fts, like
