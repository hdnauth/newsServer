"""RSS/Atom 수집기.

``kind: rss``        — 고정 URL 피드
``kind: rss_search`` — 종목별 검색 피드. ``url`` 에 ``{query}`` 자리표시자를 둔다
                       (``options.query_template``, 기본 ``"{name}"``)

옵션
  symbol_tags: category_tickers   <category> 에서 티커를 뽑아 종목 태그로 쓴다
  symbol_tag_market: US           위 태그의 시장 (기본: 소스 market)
  title_publisher_suffix: true    제목 끝 " - 언론사" 를 떼어 extra.publisher 로 옮긴다
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote_plus

import feedparser

from newsserver.collectors.base import Collector, FetchResult, RawItem, SymbolTag, SymbolTarget
from newsserver.matching import extract_ticker_tags
from newsserver.textutil import clean_text
from newsserver.timeutil import parse_feed_datetime

SUMMARY_LIMIT = 1000


class RSSCollector(Collector):
    kind = "rss"

    def missing_config(self) -> str | None:
        return None if self.spec.url else "url 미설정"

    async def fetch(self, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        resp = await self._get(self.spec.url, etag=etag, last_modified=last_modified)
        if resp.status_code == 304:
            return FetchResult(http_status=304, not_modified=True, etag=etag, last_modified=last_modified)
        items = await self._parse(resp.content)
        return FetchResult(
            items=items,
            http_status=resp.status_code,
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
        )

    async def _parse(self, content: bytes) -> list[RawItem]:
        feed = await asyncio.to_thread(feedparser.parse, content)
        opts = self.spec.options
        tag_market = str(opts.get("symbol_tag_market") or self.spec.market)
        items: list[RawItem] = []

        for entry in feed.entries[: self.spec.max_entries]:
            title = clean_text(entry.get("title", ""))
            url = (entry.get("link") or "").strip()
            if not title or not url:
                continue

            summary_raw = entry.get("summary") or entry.get("description") or ""
            if not summary_raw and entry.get("content"):
                summary_raw = entry["content"][0].get("value") or ""
            summary = clean_text(summary_raw, SUMMARY_LIMIT)

            raw_date = entry.get("published") or entry.get("updated") or entry.get("dc_date")
            parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            published = parse_feed_datetime(raw_date, parsed, self.spec.naive_tz)

            extra: dict = {}
            if opts.get("title_publisher_suffix") and " - " in title:
                title, publisher = title.rsplit(" - ", 1)
                extra["publisher"] = publisher.strip()
                title = title.strip()
            # 제목만 반복하는 요약은 정보가 없다
            if summary and summary.strip() == title:
                summary = ""

            symbols: list[SymbolTag] = []
            if opts.get("symbol_tags") == "category_tickers":
                symbols = [SymbolTag(tag_market, t, "source_tag") for t in extract_ticker_tags(entry.get("tags"))]

            items.append(RawItem(title=title, url=url, summary=summary, published_at=published,
                                 symbols=symbols, extra=extra))
        return items


class RSSSearchCollector(RSSCollector):
    kind = "rss_search"

    def missing_config(self) -> str | None:
        if "{query}" not in self.spec.url:
            return "url 에 {query} 자리표시자가 없습니다"
        return None

    async def fetch_symbol(self, target: SymbolTarget) -> FetchResult:
        template = str(self.spec.options.get("query_template") or '"{name}"')
        query = template.format(name=target.primary_name, symbol=target.symbol)
        resp = await self._get(self.spec.url.replace("{query}", quote_plus(query)))
        return FetchResult(items=await self._parse(resp.content), http_status=resp.status_code)
