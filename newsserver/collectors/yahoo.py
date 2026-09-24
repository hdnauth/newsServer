"""Yahoo Finance 종목별 뉴스 수집기 (``kind: yahoo_symbol``).

검색 API 의 news 항목을 쓴다. 항목마다 ``relatedTickers`` 가 붙어 있어 종목 태그로 쓴다.
본문 요약은 없다(title_only).

옵션: news_count (기본 20)
"""
from __future__ import annotations

import datetime as dt
import re

from newsserver.collectors.base import Collector, FetchResult, RawItem, SymbolTag, SymbolTarget
from newsserver.markets import normalize_symbol
from newsserver.textutil import clean_text

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
        resp = await self._get(YAHOO_SEARCH_URL, params={
            "q": query, "quotesCount": 0,
            "newsCount": int(self.spec.options.get("news_count", 20)),
        })
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
