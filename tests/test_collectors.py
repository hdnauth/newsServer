import datetime as dt
import json

import httpx
import pytest

from newsserver.collectors.base import CollectorError, SymbolTarget
from newsserver.collectors.dart import DartCollector
from newsserver.collectors.edgar import EdgarCollector
from newsserver.collectors.rss import RSSCollector, RSSSearchCollector
from newsserver.collectors.yahoo import YahooSymbolCollector
from newsserver.sources import SourceSpec

from .conftest import FIXTURES

UTC = dt.timezone.utc


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def spec(**kw) -> SourceSpec:
    base = dict(key="s", kind="rss", label="S", market="KR", lang="ko", url="https://feed.test/rss")
    return SourceSpec(**{**base, **kw})


async def test_rss_parses_naive_kst_and_cleans_html(settings):
    body = (FIXTURES / "rss_kst_naive.xml").read_bytes()
    async with client_for(lambda req: httpx.Response(200, content=body, headers={"ETag": '"v1"'})) as http:
        result = await RSSCollector(spec(naive_tz="Asia/Seoul"), settings, http).fetch()
    assert result.etag == '"v1"'
    assert len(result.items) == 2  # 제목 없는 항목은 버린다
    first = result.items[0]
    assert first.title == "코스피, 외국인 매수에 상승 <종합>"
    assert first.summary == "S&P 500 지수와 함께 삼성전자가 올랐다."
    assert first.published_at == dt.datetime(2026, 9, 24, 9, 18, 41, tzinfo=UTC)
    assert result.items[1].published_at is None


async def test_rss_conditional_get_304(settings):
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx.Response(304)

    async with client_for(handler) as http:
        result = await RSSCollector(spec(), settings, http).fetch(etag='"v1"', last_modified="Thu, 24 Sep 2026")
    assert result.not_modified and result.items == []
    assert seen["if-none-match"] == '"v1"'


async def test_rss_http_error_raises(settings):
    async with client_for(lambda req: httpx.Response(503)) as http:
        with pytest.raises(CollectorError):
            await RSSCollector(spec(), settings, http).fetch()


async def test_rss_category_ticker_tags_and_publisher_suffix(settings):
    body = (FIXTURES / "rss_tags.xml").read_bytes()
    s = spec(market="US", lang="en", options={"symbol_tags": "category_tickers", "title_publisher_suffix": True})
    async with client_for(lambda req: httpx.Response(200, content=body)) as http:
        items = (await RSSCollector(s, settings, http).fetch()).items
    assert [(t.market, t.symbol) for t in items[0].symbols] == [("US", "NVDA"), ("US", "AMD")]
    assert items[0].published_at == dt.datetime(2026, 9, 24, 10, 40, tzinfo=UTC)
    assert items[1].title == "삼성전자 주가 급등"
    assert items[1].extra["publisher"] == "한겨레"


async def test_rss_search_fills_query(settings):
    urls = []

    def handler(req):
        urls.append(str(req.url))
        return httpx.Response(200, content=b"<rss><channel></channel></rss>")

    s = spec(kind="rss_search", url="https://news.test/rss?q={query}&hl=ko", per_symbol=True)
    async with client_for(handler) as http:
        c = RSSSearchCollector(s, settings, http)
        assert c.missing_config() is None
        await c.fetch_symbol(SymbolTarget("KR", "005930", ["삼성전자"]))
    assert "q=%22%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90%22" in urls[0]


async def test_edgar_extracts_cik(settings):
    settings.edgar_user_agent = "Test test@example.com"
    body = (FIXTURES / "edgar.xml").read_bytes()
    uas = []

    def handler(req):
        uas.append(req.headers["user-agent"])
        return httpx.Response(200, content=body)

    s = spec(kind="edgar", market="US", lang="en", is_filing=True, body_kind="metadata", options={"forms": ["8-K"]})
    async with client_for(handler) as http:
        items = (await EdgarCollector(s, settings, http).fetch()).items
    assert uas == ["Test test@example.com"]
    assert items[0].extra == {"form": "8-K", "company": "NVIDIA CORP", "cik": "1045810", "role": "Filer"}
    assert items[0].summary.startswith("Filed: 2026-09-24")
    assert items[0].published_at == dt.datetime(2026, 9, 24, 10, 5, 12, tzinfo=UTC)


async def test_edgar_requires_user_agent(settings):
    async with client_for(lambda req: httpx.Response(200)) as http:
        assert EdgarCollector(spec(kind="edgar"), settings, http).missing_config()


def dart_page(rows, total_page=1, status="000"):
    return {"status": status, "total_page": total_page, "list": rows}


def dart_row(no, code="005930"):
    return {"rcept_no": no, "corp_name": "삼성전자", "report_nm": "주요사항보고서 ", "corp_cls": "Y",
            "stock_code": code, "flr_nm": "삼성전자", "rm": "유", "rcept_dt": "20260923", "corp_code": "00126380"}


async def test_dart_paging_and_seen_stop(settings):
    settings.dart_api_key = "k"
    pages = {1: dart_page([dart_row("3"), dart_row("2", code="")], total_page=2), 2: dart_page([dart_row("1")], 2)}
    calls = []

    def handler(req):
        page = int(req.url.params["page_no"])
        calls.append(page)
        return httpx.Response(200, json=pages[page])

    s = spec(kind="dart", is_filing=True, body_kind="metadata")
    async with client_for(handler) as http:
        c = DartCollector(s, settings, http)
        items = (await c.fetch()).items
        assert [i.extra["rcept_no"] for i in items] == ["3", "2", "1"]
        assert items[0].symbols[0].symbol == "005930" and items[0].symbols[0].method == "dart_code"
        assert items[1].symbols == []
        assert items[0].published_at is None  # 접수일만 있으므로 발행 시각은 비운다
        assert items[0].title == "[삼성전자] 주요사항보고서"
        # 두 번째 폴링: 1페이지가 전부 본 공시면 거기서 멈춘다
        calls.clear()
        assert (await c.fetch()).items == []
        assert calls == [1]


async def test_dart_no_data_and_error(settings):
    settings.dart_api_key = "k"
    s = spec(kind="dart")
    async with client_for(lambda req: httpx.Response(200, json=dart_page([], status="013"))) as http:
        assert (await DartCollector(s, settings, http).fetch()).items == []
    async with client_for(lambda req: httpx.Response(200, json={"status": "020", "message": "limit"})) as http:
        with pytest.raises(CollectorError):
            await DartCollector(s, settings, http).fetch()


async def test_yahoo_symbol(settings):
    payload = {"news": [
        {"title": "Nvidia beats", "link": "https://finance.yahoo.com/n/1", "publisher": "Reuters",
         "providerPublishTime": 1790240000, "relatedTickers": ["NVDA", "005930.KS", "^GSPC", "BRK.B"]},
        {"title": "", "link": "https://x"},
    ]}
    async with client_for(lambda req: httpx.Response(200, content=json.dumps(payload))) as http:
        s = spec(kind="yahoo_symbol", market="US", per_symbol=True)
        items = (await YahooSymbolCollector(s, settings, http).fetch_symbol(SymbolTarget("US", "NVDA"))).items
    assert len(items) == 1
    assert {(t.market, t.symbol) for t in items[0].symbols} == {("US", "NVDA"), ("KR", "005930"), ("US", "BRK-B")}
    assert items[0].published_at == dt.datetime.fromtimestamp(1790240000, UTC)
