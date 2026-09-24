import datetime as dt

import httpx
import pytest

from newsclient import NewsClient, SyncNewsClient, build_news_body
from newsserver.collectors.base import RawItem
from newsserver.main import create_app
from newsserver.timeutil import utcnow


@pytest.fixture
async def served(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        svc = app.state.services
        await svc.ingestor.ingest(svc.specs["yonhap_market"], [
            RawItem("삼성전자 HBM 양산", "https://t/1", "메모리 공급 확대", utcnow() - dt.timedelta(hours=2)),
            RawItem("국고채 금리 하락", "https://t/2", "", utcnow() - dt.timedelta(hours=1)),
        ])
        yield app


async def test_async_client_roundtrip(served):
    async with NewsClient("http://test", client="t", transport=httpx.ASGITransport(app=served)) as nc:
        items = await nc.symbol_headlines("005930", "KR", hours=24, names=["삼성전자"])
        assert [h.title for h in items] == ["삼성전자 HBM 양산"]
        h = items[0]
        assert h.source == "yonhap_market" and h.has_body and h.ts.tzinfo is not None
        assert [h.title for h in await nc.topic_headlines(["bond"])] == ["국고채 금리 하락"]

        cursor = 0
        new, cursor = await nc.since(cursor)
        assert len(new) == 2 and cursor == max(h.id for h in new)
        assert await nc.since(cursor) == ([], cursor)
        assert await nc.latest_id() == cursor

        by = await nc.by_symbols([("005930", "KR")], hours=24)
        assert list(by) == ["KR:005930"]
        assert await nc.topics_for("KODEX 미국나스닥100") == ["nasdaq", "bigtech"]
        assert await nc.set_watchlist("t", [("NVDA", "US")])
        assert (await nc.health())["status"] == "ok"


async def test_async_client_fail_soft_and_raise():
    def down(request):
        raise httpx.ConnectError("refused")

    async with NewsClient("http://x", transport=httpx.MockTransport(down)) as nc:
        assert await nc.headlines(limit=5) == []
        page = await nc.headlines_page(since_id=7)
        assert not page.ok and page.next_since_id == 7
        assert await nc.since(7) == ([], 7)
        assert (await nc.health())["status"] == "unreachable"

    async with NewsClient("http://x", transport=httpx.MockTransport(down), raise_errors=True) as nc:
        with pytest.raises(httpx.ConnectError):
            await nc.headlines()


def test_sync_client_fail_soft():
    with SyncNewsClient("http://x", transport=httpx.MockTransport(lambda r: httpx.Response(500))) as nc:
        assert nc.symbol_headlines("NVDA", "US") == []


async def test_build_news_body(served):
    async with NewsClient("http://test", transport=httpx.ASGITransport(app=served)) as nc:
        items = await nc.headlines()
    text = build_news_body(items, max_items=2, now=items[0].ts + dt.timedelta(hours=3))
    assert "[1] [3h전] 국고채 금리 하락" in text
    assert "(이 소스는 제목만 제공 — 본문 없음)" in text
    assert "출처: 연합뉴스 증권" in text
