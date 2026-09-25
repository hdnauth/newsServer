import asyncio
import datetime as dt
import time

import httpx

from newsserver.collectors.base import FetchResult, RawItem
from newsserver.scheduler import base_interval_sec, fail_backoff_sec, idle_backoff_sec
from newsserver.sources import SourceSpec
from newsserver.timeutil import parse_iso, to_iso, utcnow

UTC = dt.timezone.utc


def test_backoff_curves():
    assert [idle_backoff_sec(n) for n in (0, 4, 5, 10, 20)] == [0, 0, 600, 900, 1800]
    assert [fail_backoff_sec(n) for n in (0, 1, 2, 3, 20)] == [0, 60, 120, 240, 1800]


def test_base_interval_market_aware_and_fixed():
    s = SourceSpec(key="k", kind="rss", label="", market="KR", lang="ko", schedule="market_aware")
    open_kst = dt.datetime(2026, 9, 24, 1, 0, tzinfo=UTC)  # 목 10:00 KST
    night_kst = dt.datetime(2026, 9, 24, 14, 0, tzinfo=UTC)  # 목 23:00 KST
    assert base_interval_sec(s, open_kst) == 180
    assert base_interval_sec(s, night_kst) == 3600
    s.schedule, s.interval_sec = "fixed", 900
    assert base_interval_sec(s, open_kst) == 900


class FakeCollector:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def missing_config(self):
        return None

    async def fetch(self, etag=None, last_modified=None):
        self.calls.append((etag, last_modified))
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


async def test_run_source_updates_state_and_backoff(app_ctx):
    client, svc = app_ctx
    sched = svc.scheduler
    fake = FakeCollector([
        FetchResult(items=[RawItem("금리 동결", "https://t/1", published_at=utcnow())], http_status=200, etag='"a"'),
        FetchResult(http_status=304, not_modified=True),
        httpx.ConnectError("down"),
    ])
    sched.collectors["yonhap_economy"] = fake

    stats = await sched.run_source_now("yonhap_economy")
    st = sched.state["yonhap_economy"]
    assert stats.n_new == 1 and st.etag == '"a"' and st.idle_streak == 0 and st.last_new_at

    await sched.run_source_now("yonhap_economy")
    assert fake.calls[1] == ('"a"', None)  # 조건부 GET 헤더 전달
    assert st.etag == '"a"' and st.idle_streak == 1

    assert await sched.run_source_now("yonhap_economy") is None
    assert st.fail_streak == 1 and "ConnectError" in st.last_error
    wait = (parse_iso(st.next_poll_at) - parse_iso(st.last_fetch_at)).total_seconds()
    assert wait >= 60

    # 상태는 DB 에 남고 수집 이력도 기록된다
    async with svc.db.read() as conn:
        row = await (await conn.execute("SELECT fail_streak, etag FROM sources WHERE key = 'yonhap_economy'")).fetchone()
        n_log = (await (await conn.execute("SELECT COUNT(*) FROM fetch_log")).fetchone())[0]
    assert tuple(row) == (1, '"a"') and n_log == 3

    resp = (await client.post("/v1/sources/yonhap_economy/refresh")).json()
    assert resp == {"ok": False, "error": resp["error"]}  # 가짜 수집기 결과 소진 → 실패 보고


async def test_health_degraded_when_all_sources_fail(app_ctx):
    _, svc = app_ctx
    sched = svc.scheduler
    assert sched.health()["status"] == "ok"
    for key in sched.specs:
        sched.state[key].fail_streak = 3
    health = sched.health()
    assert health["status"] == "degraded" and health["problems"]


async def test_purge_expired_respects_source_retention(app_ctx):
    _, svc = app_ctx
    old = utcnow() - dt.timedelta(days=100)
    async with svc.db.write() as conn:
        for key, url in (("yonhap_sports", "https://t/sports"), ("yonhap_market", "https://t/market")):
            await conn.execute(
                "INSERT INTO articles(url, url_key, title, body_kind, source_key, market, lang, collected_at, ts, title_key) "
                "VALUES (?, ?, 't', 'summary', ?, 'KR', 'ko', ?, ?, 'k')",
                (url, url, key, to_iso(old), to_iso(old)),
            )
    deleted = await svc.scheduler.purge_expired()
    # sports 는 retention_days: 90, market 은 기본값(365)
    assert deleted == {"yonhap_sports": 1}
    async with svc.db.read() as conn:
        n_fts = (await (await conn.execute("SELECT COUNT(*) FROM articles_fts")).fetchone())[0]
    assert n_fts == 1  # FTS 인덱스도 함께 정리


async def test_refresh_symbol_cooldown(app_ctx):
    _, svc = app_ctx
    sched = svc.scheduler

    class FakeSymbol:
        calls = 0

        def missing_config(self):
            return None

        async def fetch_symbol(self, target):
            FakeSymbol.calls += 1
            return FetchResult(items=[RawItem("Nvidia news", f"https://y/{FakeSymbol.calls}", published_at=utcnow())])

    sched.collectors["yahoo_symbol"] = FakeSymbol()
    first = await sched.refresh_symbol("US", "NVDA", [])
    assert first == {"yahoo_symbol": "ok:1"}
    second = await sched.refresh_symbol("US", "NVDA", [])
    assert second == {"yahoo_symbol": "cooldown"} and FakeSymbol.calls == 1
    # KR 종목은 yahoo_symbol(symbol_markets: [US]) 대상이 아니고 gnews 는 기본 비활성
    assert await sched.refresh_symbol("KR", "005930", []) == {}


async def test_backup_is_gzipped_and_rotated(app_ctx):
    import gzip
    import sqlite3

    _, svc = app_ctx
    svc.settings.backup_keep = 2
    for tag in ("2026-01-01", "2026-01-02", "2026-01-03"):
        await svc.scheduler.backup(tag)
    files = sorted(p.name for p in svc.settings.backup_dir.iterdir())
    assert files == ["news-2026-01-02.db.gz", "news-2026-01-03.db.gz"]
    restored = svc.settings.backup_dir / "restored.db"
    restored.write_bytes(gzip.decompress((svc.settings.backup_dir / files[-1]).read_bytes()))
    with sqlite3.connect(restored) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1


class PacedFakeSymbol:
    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.calls: list[tuple[str, float]] = []

    def missing_config(self):
        return None

    async def fetch_symbol(self, target):
        self.calls.append((target.symbol, time.monotonic()))
        await asyncio.sleep(self.delay)
        return FetchResult(items=[RawItem(f"{target.symbol} news", f"https://y/{target.symbol}", published_at=utcnow())])


async def test_refresh_symbol_respects_request_gap_across_symbols(app_ctx):
    _, svc = app_ctx
    sched = svc.scheduler
    sched.specs["yahoo_symbol"].options["request_gap_sec"] = 0.2
    fake = PacedFakeSymbol()
    sched.collectors["yahoo_symbol"] = fake

    results = await asyncio.gather(*(sched.refresh_symbol("US", s, []) for s in ("AAPL", "MSFT", "NVDA")))
    assert all(r == {"yahoo_symbol": "ok:1"} for r in results)
    starts = sorted(t for _, t in fake.calls)
    assert len(starts) == 3
    assert all(b - a >= 0.19 for a, b in zip(starts, starts[1:]))  # 동시에 와도 간격을 두고 한 건씩


async def test_refresh_symbol_joins_inflight_fetch(app_ctx):
    _, svc = app_ctx
    sched = svc.scheduler
    fake = PacedFakeSymbol(delay=0.1)
    sched.collectors["yahoo_symbol"] = fake

    first, second = await asyncio.gather(sched.refresh_symbol("US", "NVDA", []),
                                         sched.refresh_symbol("US", "NVDA", []))
    assert len(fake.calls) == 1  # 쿨다운 기록 전에 겹친 요청도 원격은 한 번만
    assert first == second == {"yahoo_symbol": "ok:1"}
    assert not sched._symbol_inflight
