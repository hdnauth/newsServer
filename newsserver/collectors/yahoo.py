"""Yahoo Finance 종목별 뉴스 수집기 (``kind: yahoo_symbol``).

기본은 Yahoo Finance 웹의 종목 뉴스 스트림(``/xhr/ncp``)을 쓴다. 항목마다 요약이 있다.
관련 종목 목록은 주지 않으므로 종목 태그는 수집 대상 종목과 사전 태깅으로 붙는다.

스트림 호출이 실패하거나 응답 구조가 달라지면 같은 수집 주기 안에서 검색 API 로 폴백한다.
검색 API 는 요약이 없는 대신 ``relatedTickers`` 가 있어 종목 태그로 쓴다.
정상일 때는 종목당 한 번만 요청한다.

옵션: news_count (기본 20)
"""
from __future__ import annotations

import datetime as dt
import re

from loguru import logger

from newsserver.collectors.base import Collector, CollectorError, FetchResult, RawItem, SymbolTag, SymbolTarget
from newsserver.markets import normalize_symbol
from newsserver.textutil import clean_text
from newsserver.timeutil import parse_iso

YAHOO_NEWS_STREAM_URL = "https://finance.yahoo.com/xhr/ncp"
YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"


def _ticker_to_key(ticker: str) -> tuple[str, str] | None:
    t = (ticker or "").strip().upper()
    if not t:
        return None
    if t.endswith((".KS", ".KQ")):
        return "KR", normalize_symbol(t, "KR")
    if "." in t and t.split(".")[-1] not in ("A", "B", "C"):
        return None  # 기타 해외 거래소
    t = t.replace(".", "-")
    return ("US", t) if re.fullmatch(r"[A-Z][A-Z0-9]{0,5}(?:-[A-Z])?", t) else None


class YahooSymbolCollector(Collector):
    kind = "yahoo_symbol"

    async def fetch_symbol(self, target: SymbolTarget) -> FetchResult:
        query = f"{target.symbol}.KS" if target.market == "KR" else target.symbol
        count = int(self.spec.options.get("news_count", 20))
        try:
            return await self._fetch_stream(query, count)
        except (CollectorError, ValueError) as e:
            logger.warning("{} [{}] 뉴스 스트림 실패, 검색 API 로 폴백: {}", self.spec.key, query, e)
            return await self._fetch_search(query, count)

    async def _fetch_stream(self, query: str, count: int) -> FetchResult:
        resp = await self.http.post(
            YAHOO_NEWS_STREAM_URL, params={"queryRef": "latestNews", "serviceKey": "ncp_fin"},
            json={"serviceConfig": {"snippetCount": count, "s": [query]}},
        )
        if resp.status_code != 200:
            raise CollectorError(f"HTTP {resp.status_code}", resp.status_code)
        stream = (((resp.json() or {}).get("data") or {}).get("tickerStream") or {}).get("stream")
        if not isinstance(stream, list):
            raise ValueError("응답에 data.tickerStream.stream 없음")
        items: list[RawItem] = []
        for entry in stream:
            if not isinstance(entry, dict) or entry.get("ad"):
                continue
            content = entry.get("content")
            if not isinstance(content, dict):
                continue
            title = clean_text(content.get("title"))
            # 검색 API 의 link 와 같은 값이라 이전에 수집한 기사와 URL 로 병합된다
            url = ((content.get("clickThroughUrl") or {}).get("url")
                   or (content.get("canonicalUrl") or {}).get("url") or "").strip()
            if not title or not url:
                continue
            items.append(RawItem(
                title=title, url=url,
                summary=content.get("summary") or clean_text(content.get("description")),
                published_at=parse_iso(content.get("pubDate") or content.get("displayTime")),
                extra={"publisher": (content.get("provider") or {}).get("displayName") or "",
                       "content_type": content.get("contentType") or ""},
            ))
            if len(items) >= self.spec.max_entries:
                break
        return FetchResult(items=items, http_status=resp.status_code)

    async def _fetch_search(self, query: str, count: int) -> FetchResult:
        resp = await self._get(YAHOO_SEARCH_URL, params={"q": query, "quotesCount": 0, "newsCount": count})
        items: list[RawItem] = []
        for news in (resp.json().get("news") or [])[: self.spec.max_entries]:
            title = clean_text(news.get("title"))
            url = (news.get("link") or "").strip()
            if not title or not url:
                continue
            published = None
            if news.get("providerPublishTime"):
                published = dt.datetime.fromtimestamp(int(news["providerPublishTime"]), dt.timezone.utc)
            symbols = [SymbolTag(*key, "source_tag") for key in
                       filter(None, (_ticker_to_key(t) for t in news.get("relatedTickers") or []))]
            items.append(RawItem(title=title, url=url, published_at=published, symbols=symbols,
                                 extra={"publisher": news.get("publisher") or ""}))
        return FetchResult(items=items, http_status=resp.status_code)
