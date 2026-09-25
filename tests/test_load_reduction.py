"""수집·저장 부하 절감 변경의 동작 — 피드 기준 보관, 분류 필터, 종목별 주기, 수집 시 태깅 생략,
증분 재태깅, 사전 갱신, 락 없는 백업."""
import asyncio
import datetime as dt
import gzip
import sqlite3

import pytest

from newsserver.collectors.base import FetchResult, RawItem, SymbolTag, SymbolTarget
from newsserver.scheduler import symbol_interval_sec
from newsserver.sources import SourceSpec, load_sources
from newsserver.symbols import SymbolDirectory, SymbolEntry
from newsserver.timeutil import parse_iso, to_iso, utcnow

UTC = dt.timezone.utc
ET = dt.timezone(dt.timedelta(hours=-4))  # 9월 미국 동부 서머타임


def item(title, url, summary="", hours_ago=1.0, **kw) -> RawItem:
    return RawItem(title=title, url=url, summary=summary,
                   published_at=utcnow() - dt.timedelta(hours=hours_ago), **kw)


async def ingest(svc, key, items, target=None):
    return await svc.ingestor.ingest(svc.specs[key], items, target=target)


async def age(svc, url_key_like: str, days: float) -> None:
    ts = to_iso(utcnow() - dt.timedelta(days=days))
    async with svc.db.write() as conn:
        await conn.execute("UPDATE articles SET ts = ?, published_at = ?, collected_at = ? WHERE url LIKE ?",
                           (ts, ts, ts, url_key_like))


async def urls(svc) -> set[str]:
    async with svc.db.read() as conn:
        return {r[0] for r in await (await conn.execute("SELECT url FROM articles")).fetchall()}


async def tag_rows(svc) -> set[tuple]:
    async with svc.db.read() as conn:
        cur = await conn.execute("SELECT article_id, market, symbol, method FROM article_symbols")
        symbols = {tuple(r) for r in await cur.fetchall()}
        cur = await conn.execute("SELECT article_id, topic FROM article_topics")
        return symbols | {tuple(r) for r in await cur.fetchall()}


# ── 피드 기준 보관 ────────────────────────────────────────────────────────────
async def test_purge_uses_longest_retention_among_feeds(app_ctx):
    _, svc = app_ctx
    # 일반 피드(90일)가 먼저 가져간 경제 기사 — 경제 피드(365일)에도 실렸다
    await ingest(svc, "yonhap_all", [item("금리 동결", "https://yna.test/econ"),
                                     item("축구 승리", "https://yna.test/sports"),
                                     item("사건 사고", "https://yna.test/general")])
    await ingest(svc, "yonhap_economy", [item("금리 동결", "https://yna.test/econ")])
    await ingest(svc, "yonhap_sports", [item("축구 승리", "https://yna.test/sports")])
    # 종목별 뉴스(120일)
    await ingest(svc, "yahoo_symbol", [item("Nvidia A", "https://y.test/100"), item("Nvidia B", "https://y.test/130")],
                 target=SymbolTarget("US", "NVDA"))
    for pattern in ("https://yna.test/%",):
        await age(svc, pattern, 100)
    await age(svc, "https://y.test/100", 100)
    await age(svc, "https://y.test/130", 130)

    deleted = await svc.scheduler.purge_expired()
    assert await urls(svc) == {"https://yna.test/econ", "https://y.test/100"}
    assert deleted == {"yonhap_all": 2, "yahoo_symbol": 1}
    async with svc.db.read() as conn:
        assert (await (await conn.execute("SELECT COUNT(*) FROM articles_fts")).fetchone())[0] == 2
        assert (await (await conn.execute("SELECT COUNT(*) FROM article_feeds")).fetchone())[0] == 3


async def test_purge_financial_feed_whitelist_still_sees_kept_article(app_ctx):
    """피드 소속(sources=)으로 조회하는 소비자는 보관된 기사를 그대로 받는다."""
    client, svc = app_ctx
    await ingest(svc, "yonhap_all", [item("금리 동결", "https://yna.test/econ")])
    await ingest(svc, "yonhap_economy", [item("금리 동결", "https://yna.test/econ")])
    await age(svc, "https://yna.test/%", 200)
    await svc.scheduler.purge_expired()
    body = (await client.get("/v1/headlines", params={"sources": "yonhap_economy", "hours": 24 * 300})).json()
    assert [i["url"] for i in body["items"]] == ["https://yna.test/econ"]
    await age(svc, "https://yna.test/%", 400)
    await svc.scheduler.purge_expired()
    assert await urls(svc) == set()


# ── 분류 필터 ────────────────────────────────────────────────────────────────
async def test_category_filter_includes_articles_first_collected_by_general_feed(app_ctx):
    client, svc = app_ctx
    await ingest(svc, "yonhap_all", [item("축구 대표팀 승리", "https://yna.test/s1"),
                                     item("정부 발표", "https://yna.test/g1")])
    await ingest(svc, "yonhap_sports", [item("축구 대표팀 승리", "https://yna.test/s1"),
                                        item("야구 개막", "https://yna.test/s2")])
    sports = (await client.get("/v1/headlines", params={"category": "sports"})).json()["items"]
    assert {i["url"] for i in sports} == {"https://yna.test/s1", "https://yna.test/s2"}
    general = (await client.get("/v1/headlines", params={"category": "general"})).json()["items"]
    assert {i["url"] for i in general} == {"https://yna.test/s1", "https://yna.test/g1"}
    # 기사의 category 필드 자체는 그대로(처음 수집한 피드의 것)
    assert {i["url"]: i["category"] for i in sports}["https://yna.test/s1"] == "general"


# ── 종목별 소스 주기 ─────────────────────────────────────────────────────────
def _yahoo(svc_or_none=None) -> SourceSpec:
    from newsserver.config import ROOT
    return {s.key: s for s in load_sources(ROOT / "config" / "sources.yaml")}["yahoo_symbol"]


@pytest.mark.parametrize("local, expected", [
    (dt.datetime(2026, 9, 24, 10, 0, tzinfo=ET), 3600),   # 목 정규장
    (dt.datetime(2026, 9, 24, 7, 0, tzinfo=ET), 3600),    # 목 프리마켓
    (dt.datetime(2026, 9, 24, 17, 0, tzinfo=ET), 3600),   # 목 애프터마켓 (실적 발표)
    (dt.datetime(2026, 9, 24, 21, 0, tzinfo=ET), 10800),  # 목 야간
    (dt.datetime(2026, 9, 25, 2, 30, tzinfo=ET), 5400),   # 금 새벽 — 04:00 프리마켓 시작에서 끊는다
    (dt.datetime(2026, 9, 26, 12, 0, tzinfo=ET), 10800),  # 토
    (dt.datetime(2026, 9, 28, 2, 0, tzinfo=ET), 7200),    # 월 새벽 — 04:00 까지
])
def test_symbol_interval_follows_target_market_session(local, expected):
    assert symbol_interval_sec(_yahoo(), "US", local.astimezone(UTC)) == expected


def test_symbol_interval_fixed_and_market_of_target():
    spec = _yahoo()
    kst_open = dt.datetime(2026, 9, 24, 1, 0, tzinfo=UTC)  # 목 10:00 KST, 미국은 21:00 ET(야간)
    assert symbol_interval_sec(spec, "US", kst_open) == 10800
    assert symbol_interval_sec(spec, "KR", kst_open) == 3600
    fixed = SourceSpec(key="g", kind="rss_search", label="", market="KR", lang="ko", schedule="fixed",
                       interval_sec=3600, per_symbol=True)
    assert symbol_interval_sec(fixed, "KR", kst_open) == 3600


def test_per_symbol_market_aware_requires_explicit_intervals(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text("defaults: {schedule: market_aware}\nsources:\n"
                    "  - {key: y, kind: yahoo_symbol, label: y, market: US, lang: en, per_symbol: true}\n")
    with pytest.raises(ValueError, match="market_intervals"):
        load_sources(path)
    path.write_text("defaults: {schedule: market_aware}\nsources:\n"
                    "  - {key: y, kind: yahoo_symbol, label: y, market: US, lang: en, per_symbol: true,\n"
                    "     market_intervals: {open: 3600, extended: 3600, closed: 10800}}\n")
    assert load_sources(path)[0].market_intervals["closed"] == 10800


def test_real_sources_yaml_values():
    from newsserver.config import ROOT
    specs = {s.key: s for s in load_sources(ROOT / "config" / "sources.yaml")}
    assert specs["yonhap_all"].retention_days == 90
    general = ("yonhap_politics", "yonhap_society", "yonhap_international", "yonhap_entertainment", "yonhap_sports")
    assert all(specs[k].retention_days == 90 and specs[k].schedule == "fixed" for k in general)
    assert specs["yahoo_symbol"].retention_days == 120
    assert specs["gnews_kr_symbol"].schedule == "fixed" and specs["gnews_kr_symbol"].interval_sec == 3600
    edgar = specs["edgar_8k"]
    assert edgar.max_entries == edgar.options["count"]
    # 금융 피드는 기본 보관(365일) 그대로
    assert all(specs[k].retention_days is None for k in
               ("yonhap_economy", "yonhap_market", "einfomax_all", "mk_economy", "seeking_alpha", "dart"))


async def test_symbol_job_schedules_next_poll_by_session(app_ctx):
    _, svc = app_ctx
    sched = svc.scheduler

    class Fake:
        def missing_config(self):
            return None

        async def fetch_symbol(self, target):
            return FetchResult(items=[])

    sched.collectors["yahoo_symbol"] = Fake()
    before = utcnow()
    await sched.refresh_symbol("US", "NVDA", [])
    st = sched.symbol_state[("yahoo_symbol", "US", "NVDA")]
    wait = (parse_iso(st.next_poll_at) - before).total_seconds()
    assert abs(wait - symbol_interval_sec(svc.specs["yahoo_symbol"], "US", before)) <= 5


# ── 수집 시 태깅 ─────────────────────────────────────────────────────────────
async def test_known_article_keeps_stored_text_tags_but_gets_feed_and_fixed_tags(app_ctx):
    client, svc = app_ctx
    await ingest(svc, "yahoo_finance", [item("Chip stocks rally", "https://y.test/a")])
    # 같은 기사, 다른 피드의 다른 제목 — 저장된 텍스트가 아니므로 사전 태그를 붙이지 않는다
    await ingest(svc, "seeking_alpha", [item("Nvidia leads chip stocks rally", "https://y.test/a",
                                             symbols=[SymbolTag("US", "AMD", "source_tag")])])
    await ingest(svc, "yahoo_symbol", [item("Chip stocks rally", "https://y.test/a")],
                 target=SymbolTarget("US", "AAPL"))
    art = (await client.get("/v1/headlines")).json()["items"][0]
    assert set(art["feeds"]) == {"yahoo_finance", "seeking_alpha", "yahoo_symbol"}
    assert {(s["symbol"], s["method"]) for s in art["symbols"]} == {("AMD", "source_tag"), ("AAPL", "search")}


async def test_enriched_summary_is_tagged(app_ctx):
    client, svc = app_ctx
    await ingest(svc, "hankyung_finance", [item("반도체 투자 확대", "https://hk.test/1")])
    stats = await ingest(svc, "yonhap_market", [item("반도체 투자 확대", "https://hk.test/1",
                                                     "엔비디아가 한국 반도체 기업과 협력한다")])
    assert stats.n_updated == 1
    art = (await client.get("/v1/headlines")).json()["items"][0]
    assert ("NVDA", "dict") in {(s["symbol"], s["method"]) for s in art["symbols"]}
    assert "semiconductor" in art["topics"]


async def test_ingest_tags_equal_full_retag(app_ctx):
    """수집 시 붙은 사전·주제 태그가 저장된 텍스트로 전체 재태깅한 결과와 같다."""
    _, svc = app_ctx
    await ingest(svc, "yonhap_market", [item("삼성전자 HBM 양산", "https://t/1", "SK하이닉스도 증설"),
                                        item("코스피 상승", "https://t/2")])
    await ingest(svc, "einfomax_all", [item("삼성전자 HBM 양산 (종합)", "https://t/1", "현대차 언급"),
                                       item("애플 신제품", "https://t/3", "")])
    await ingest(svc, "hankyung_finance", [item("엔비디아 실적", "https://t/4")])
    await ingest(svc, "yonhap_economy", [item("엔비디아 실적", "https://t/4", "구글과 애플도 강세")])
    # 보강된 요약의 명시적 참조가 제목의 사전 태그(dict)보다 우선한다(ref)
    await ingest(svc, "yahoo_finance", [item("Nvidia rallies", "https://t/5")])
    await ingest(svc, "cnbc_investing", [item("Nvidia rallies", "https://t/5", "(NASDAQ: NVDA) shares rose")])
    before = await tag_rows(svc)
    await svc.ingestor.retag(topics=True, symbols=True)
    assert await tag_rows(svc) == before


async def test_new_item_tagging_runs_outside_write_lock(app_ctx, monkeypatch):
    _, svc = app_ctx
    await ingest(svc, "yonhap_market", [item("삼성전자 신고가", "https://t/known")])
    seen: list[bool] = []
    real = svc.directory.tag

    def spy(title, summary=""):
        seen.append(svc.db._write_lock.locked())
        return real(title, summary)

    monkeypatch.setattr(svc.directory, "tag", spy)
    await ingest(svc, "einfomax_all", [item("삼성전자 신고가", "https://t/known"), item("현대차 수출", "https://t/new")])
    assert seen == [False]  # 신규 1건만, 락 밖에서


# ── 증분 재태깅 ──────────────────────────────────────────────────────────────
CORPUS = [
    ("삼성전자 HBM 양산", "SK하이닉스와 경쟁"),
    ("현대차 수출 호조", "기아도 동반 상승"),
    ("Nvidia and Apple lead gains", "Alphabet Inc. slips"),
    ("Target Corp beats estimates", "(NYSE: TGT) shares rose"),
    ("Buy $ZZZ now", "New ticker ZZZ) debuts"),
    ("Zeta's Globex Holdings expands", ""),  # 이름 중간 단어의 소유격 — 이름 구절 그대로는 본문에 없다
    ("신규상장 제타바이오 공모", "제타바이오는 (123456) 코스닥 상장"),
    ("하닉 실적 발표", "메모리 업황 개선"),
    ("금리 동결", "한국은행 발표"),
    ("Morgan Stanley upgrades", ""),
]


def _entries(extra: list[SymbolEntry] = (), drop: set = frozenset(), alias: dict | None = None):
    base = [
        SymbolEntry("KR", "005930", "삼성전자"),
        SymbolEntry("KR", "000660", "SK하이닉스"),
        SymbolEntry("KR", "005380", "현대자동차", aliases=["현대차"]),
        SymbolEntry("US", "NVDA", "NVIDIA CORP", rank=2),
        SymbolEntry("US", "AAPL", "Apple Inc.", rank=1),
        SymbolEntry("US", "GOOGL", "Alphabet Inc.", rank=4),
        SymbolEntry("US", "GOOG", "Alphabet Inc.", rank=5),
        SymbolEntry("US", "TGT", "TARGET CORP", rank=300),
        SymbolEntry("US", "MS", "MORGAN STANLEY", rank=30),
    ]
    out = {(e.market, e.symbol): e for e in [*base, *extra] if (e.market, e.symbol) not in drop}
    for key, aliases in (alias or {}).items():
        out[key].aliases = list(aliases)
    return out


async def _tags_after_incremental(svc, old_entries, new_entries) -> tuple[set, int]:
    d = svc.directory
    d._rebuild(old_entries)
    await svc.ingestor.retag(topics=False, symbols=True)
    before = d.snapshot()
    d._rebuild(new_entries)
    touched = await svc.ingestor.retag_changed(d.changes_since(before))
    return await tag_rows(svc), touched


@pytest.mark.parametrize("change", ["add_ticker_and_name", "remove", "rename", "alias", "short_alias",
                                    "rank_winner", "spaced_name", "kr_code_ref"])
async def test_incremental_retag_equals_full_retag(app_ctx, change):
    _, svc = app_ctx
    kr = {0, 1, 6, 7, 8}
    await ingest(svc, "yonhap_market", [item(t, f"https://c/{i}", s) for i, (t, s) in enumerate(CORPUS) if i in kr])
    await ingest(svc, "cnbc_investing", [item(t, f"https://c/{i}", s) for i, (t, s) in enumerate(CORPUS) if i not in kr])
    old = _entries()
    new = {
        "add_ticker_and_name": lambda: _entries([SymbolEntry("US", "ZZZ", "Zzz Holdings", rank=50)]),
        "remove": lambda: _entries(drop={("US", "TGT"), ("KR", "000660")}),
        "rename": lambda: {**_entries(drop={("US", "MS")}), ("US", "MS"): SymbolEntry("US", "MS", "MORGAN STANLEY WEALTH")},
        "alias": lambda: _entries(alias={("KR", "000660"): ["하이닉스"], ("US", "NVDA"): ["Nvidia"]}),
        "short_alias": lambda: _entries(alias={("KR", "000660"): ["하닉"]}),
        "rank_winner": lambda: {**_entries(), ("US", "GOOG"): SymbolEntry("US", "GOOG", "Alphabet Inc.", rank=1)},
        "spaced_name": lambda: _entries([SymbolEntry("US", "ZGH", "Zeta Globex Holdings", rank=70)]),
        "kr_code_ref": lambda: _entries([SymbolEntry("KR", "123456", "제타바이오")]),
    }[change]()
    incremental, touched = await _tags_after_incremental(svc, old, new)
    await svc.ingestor.retag(topics=False, symbols=True)
    assert incremental == await tag_rows(svc)
    assert 0 < touched < len(CORPUS)


async def test_incremental_retag_noop_when_unchanged(app_ctx):
    _, svc = app_ctx
    await ingest(svc, "yonhap_market", [item("삼성전자 HBM", "https://c/1")])
    before = svc.directory.snapshot()
    await svc.directory.load(svc.db)
    changes = svc.directory.changes_since(before)
    assert not changes and await svc.ingestor.retag_changed(changes) == 0


async def test_put_alias_retags_existing_articles(app_ctx):
    client, svc = app_ctx
    await ingest(svc, "yonhap_market", [item("하닉 실적 발표", "https://c/hanik")])
    art = (await client.get("/v1/headlines")).json()["items"][0]
    assert not any(s["symbol"] == "000660" for s in art["symbols"])
    r = await client.put("/v1/symbols/KR/000660/aliases", json={"aliases": ["하이닉스", "하닉"]})
    assert r.status_code == 200
    await asyncio.gather(*svc.scheduler._background)
    art = (await client.get("/v1/headlines")).json()["items"][0]
    assert ("000660", "dict") in {(s["symbol"], s["method"]) for s in art["symbols"]}


async def test_startup_rules_fingerprint_triggers_full_retag_once(app_ctx, monkeypatch):
    _, svc = app_ctx
    calls = []
    real = svc.ingestor.retag

    async def spy(**kw):
        calls.append(kw)
        return await real(**kw)

    monkeypatch.setattr(svc.ingestor, "retag", spy)
    await svc.scheduler._startup_jobs()
    await svc.scheduler._startup_jobs()
    assert calls.count({"topics": False, "symbols": True}) == 1
    assert await svc.db.get_meta("symbol_rules_fp") == svc.directory.rules_fingerprint()


def test_rules_fingerprint_changes_with_rules():
    a = SymbolDirectory(wordlist={"apple"}, stopnames={"코리아"})
    assert a.rules_fingerprint() == SymbolDirectory(wordlist={"apple"}, stopnames={"코리아"}).rules_fingerprint()
    assert a.rules_fingerprint() != SymbolDirectory(wordlist={"apple", "target"}, stopnames={"코리아"}).rules_fingerprint()
    assert a.rules_fingerprint() != SymbolDirectory(wordlist={"apple"}, stopnames=set()).rules_fingerprint()


# ── 사전 갱신 쓰기 ───────────────────────────────────────────────────────────
async def test_replace_origin_updates_only_changed_rows(app_ctx):
    _, svc = app_ctx
    d = svc.directory
    rows = [("US", f"T{i:03d}", f"Company {i}", "", str(i), None, i) for i in range(600)]
    await d._replace_origin(svc.db, "sec", rows)
    async with svc.db.write() as conn:
        await conn.execute("UPDATE symbols SET updated_at = '2000-01-01T00:00:00Z' WHERE origin = 'sec'")
        await conn.execute("UPDATE symbols SET aliases_json = '[\"keep\"]' WHERE symbol = 'T599'")
    changed = [*rows[:598]]
    changed[0] = ("US", "T000", "Company Zero", "", "0", None, 0)   # 이름 변경
    changed[1] = ("US", "T001", "Company 1", "", "1", None, 999)    # 순위 변경
    await d._replace_origin(svc.db, "sec", changed)                 # T598·T599 빠짐
    async with svc.db.read() as conn:
        cur = await conn.execute("SELECT symbol, name, rank, updated_at FROM symbols WHERE origin = 'sec'")
        got = {r["symbol"]: r for r in await cur.fetchall()}
    touched = {s for s, r in got.items() if r["updated_at"] != "2000-01-01T00:00:00Z"}
    assert touched == {"T000", "T001"}
    assert got["T000"]["name"] == "Company Zero" and got["T001"]["rank"] == 999
    assert "T598" not in got and "T599" in got  # 별칭 달린 항목은 남긴다
    # KR(cik NULL) 행도 NULL 비교로 매번 갱신되지 않는다
    kr = [("KR", f"{i:06d}", f"회사{i}", "", None, f"C{i}", None) for i in range(600)]
    await d._replace_origin(svc.db, "dart", kr)
    async with svc.db.write() as conn:
        await conn.execute("UPDATE symbols SET updated_at = '2000-01-01T00:00:00Z' WHERE origin = 'dart'")
    await d._replace_origin(svc.db, "dart", kr)
    async with svc.db.read() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM symbols WHERE origin = 'dart' AND updated_at != '2000-01-01T00:00:00Z'")
        assert (await cur.fetchone())[0] == 0


async def test_refresh_symbols_retags_only_changed(app_ctx, monkeypatch):
    _, svc = app_ctx
    await ingest(svc, "yonhap_market", [item("제타바이오 공모", "https://c/zeta"), item("삼성전자 신고가", "https://c/sam")])

    async def fake_remote(db, http, **kw):
        async with db.write() as conn:
            await conn.execute("INSERT INTO symbols(market, symbol, name, origin, updated_at) "
                               "VALUES ('KR', '123456', '제타바이오', 'dart', '2026-01-01T00:00:00Z')")
        return {"dart": 1}

    monkeypatch.setattr(svc.directory, "refresh_remote", fake_remote)
    touched = []
    real = svc.ingestor.retag_changed

    async def spy(changes, **kw):
        touched.append(await real(changes, **kw))
        return touched[-1]

    monkeypatch.setattr(svc.ingestor, "retag_changed", spy)
    await svc.scheduler.refresh_symbols()
    assert touched == [1]
    async with svc.db.read() as conn:
        cur = await conn.execute("SELECT a.url FROM article_symbols s JOIN articles a ON a.id = s.article_id "
                                 "WHERE s.symbol = '123456'")
        assert [r[0] for r in await cur.fetchall()] == ["https://c/zeta"]


# ── 백업 ─────────────────────────────────────────────────────────────────────
async def test_backup_does_not_take_write_lock_and_is_consistent(app_ctx):
    _, svc = app_ctx
    await ingest(svc, "yonhap_market", [item(f"기사 {i}", f"https://b/{i}") for i in range(50)])
    async with svc.db.write():
        # 수집이 쓰기 락을 잡고 있는 동안에도 백업이 끝난다 (이전에는 락을 기다렸다)
        await asyncio.wait_for(svc.db.backup(svc.settings.backup_dir / "snap.db"), timeout=5)
    writer = asyncio.create_task(ingest(svc, "einfomax_all", [item(f"동시 {i}", f"https://b/c{i}") for i in range(200)]))
    await svc.scheduler.backup("2026-09-25")
    await writer
    raw = gzip.decompress((svc.settings.backup_dir / "news-2026-09-25.db.gz").read_bytes())
    restored = svc.settings.backup_dir / "restored.db"
    restored.write_bytes(raw)
    with sqlite3.connect(restored) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        n = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert n in (50, 250)  # 스냅샷 — 동시 수집의 트랜잭션은 통째로 있거나 없다
        assert conn.execute("SELECT COUNT(*) FROM articles_fts").fetchone()[0] == n


async def test_schema_v2_drops_unused_cik_index(app_ctx):
    _, svc = app_ctx
    async with svc.db.read() as conn:
        assert (await (await conn.execute("PRAGMA user_version")).fetchone())[0] == 2
        cur = await conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'ix_symbols_cik'")
        assert await cur.fetchone() is None
