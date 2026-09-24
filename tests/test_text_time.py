import datetime as dt
import time

from newsserver.markets import Session, market_session, normalize_market, normalize_symbol
from newsserver.textutil import clean_text, title_key, url_key
from newsserver.timeutil import parse_feed_datetime, sanitize_published, to_iso

UTC = dt.timezone.utc


def test_parse_rfc822_with_offset():
    got = parse_feed_datetime("Thu, 24 Sep 2026 18:09:59 +0900")
    assert got == dt.datetime(2026, 9, 24, 9, 9, 59, tzinfo=UTC)


def test_parse_naive_uses_source_timezone():
    # 타임존 표기 없는 현지 시각 — 소스 설정 시간대로 해석해야 한다
    got = parse_feed_datetime("2026-09-24 18:18:41", naive_tz="Asia/Seoul")
    assert got == dt.datetime(2026, 9, 24, 9, 18, 41, tzinfo=UTC)
    assert parse_feed_datetime("2026-09-24 18:18:41") == dt.datetime(2026, 9, 24, 18, 18, 41, tzinfo=UTC)


def test_parse_gmt_iso_and_minus_zero():
    assert parse_feed_datetime("Thu, 24 Sep 2026 10:01:00 GMT") == dt.datetime(2026, 9, 24, 10, 1, tzinfo=UTC)
    assert parse_feed_datetime("2024-03-21T17:39:15Z") == dt.datetime(2024, 3, 21, 17, 39, 15, tzinfo=UTC)
    # "-0000" 은 표기가 있는 UTC 이므로 naive_tz 를 적용하지 않는다
    got = parse_feed_datetime("Thu, 24 Sep 2026 10:01:00 -0000", naive_tz="Asia/Seoul")
    assert got == dt.datetime(2026, 9, 24, 10, 1, tzinfo=UTC)


def test_parsed_struct_fallback_is_utc_not_local():
    struct = time.strptime("2026-09-24 10:00:00", "%Y-%m-%d %H:%M:%S")
    assert parse_feed_datetime("garbage", struct) == dt.datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


def test_sanitize_published():
    now = dt.datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    assert sanitize_published(now + dt.timedelta(minutes=30), now) == now
    assert sanitize_published(now + dt.timedelta(hours=9), now) is None
    past = now - dt.timedelta(hours=2)
    assert sanitize_published(past, now) == past


def test_to_iso_format():
    assert to_iso(dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "2026-01-02T03:04:05Z"
    assert to_iso(dt.datetime(2026, 1, 2, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))) == "2026-01-02T03:00:00Z"


def test_clean_text_unescapes_entities():
    assert clean_text("<p>S&amp;P 500 &lt;b&gt;up&lt;/b&gt;</p>") == "S&P 500 up"
    assert clean_text("a" * 20, limit=5) == "aaaaa"


def test_url_key_normalization():
    a = url_key("http://www.Example.com/news/1/?utm_source=rss&b=2&a=1#top")
    b = url_key("https://m.example.com/news/1?a=1&b=2&fbclid=xyz")
    assert a == b == "https://example.com/news/1?a=1&b=2"
    assert url_key("https://example.com/view?idxno=1") != url_key("https://example.com/view?idxno=2")


def test_title_key_ignores_punctuation_and_case():
    assert title_key("삼성전자, HBM4 양산!") == title_key("삼성전자 hbm4 양산")


def test_market_normalization():
    assert normalize_market("krx") == "KR"
    assert normalize_market("NASDAQ") == "US"
    assert normalize_market("mars") is None
    assert normalize_symbol("5930", "KR") == "005930"
    assert normalize_symbol("005930.KS", "KR") == "005930"
    assert normalize_symbol("$nvda", "US") == "NVDA"


def test_market_session():
    # 2026-09-24 (목) 10:00 KST = 01:00 UTC
    assert market_session("KR", dt.datetime(2026, 9, 24, 1, 0, tzinfo=UTC)) == Session.OPEN
    # 19:00 KST — 확장 거래 시간
    assert market_session("KR", dt.datetime(2026, 9, 24, 10, 0, tzinfo=UTC)) == Session.EXTENDED
    # 10:00 ET = 14:00 UTC
    assert market_session("US", dt.datetime(2026, 9, 24, 14, 0, tzinfo=UTC)) == Session.OPEN
    # 토요일
    assert market_session("US", dt.datetime(2026, 9, 26, 14, 0, tzinfo=UTC)) == Session.CLOSED
    assert market_session("GLOBAL", dt.datetime(2026, 9, 24, 14, 0, tzinfo=UTC)) == Session.OPEN
