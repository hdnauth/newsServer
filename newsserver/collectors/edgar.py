"""SEC EDGAR 최신 제출 서류 수집기 (``kind: edgar``).

"current filings" Atom 피드. 제목 형식: ``8-K - Apple Inc. (0000320193) (Filer)``.
제목의 CIK 를 ``extra.cik`` 로 넘기면 파이프라인이 심볼 사전으로 티커를 붙인다.

옵션: forms (기본 ["8-K"]), count (기본 40)
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlencode

import feedparser

from newsserver.collectors.base import Collector, FetchResult, RawItem
from newsserver.textutil import clean_text
from newsserver.timeutil import parse_feed_datetime

EDGAR_CURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
_TITLE_RE = re.compile(r"^(?P<form>[^\s]+)\s+-\s+(?P<company>.+?)\s+\((?P<cik>\d{4,10})\)\s*(?:\((?P<role>[^)]+)\))?\s*$")


class EdgarCollector(Collector):
    kind = "edgar"

    def missing_config(self) -> str | None:
        return None if self.settings.edgar_user_agent else "EDGAR_USER_AGENT 미설정"

    async def fetch(self, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        forms = self.spec.options.get("forms") or ["8-K"]
        count = int(self.spec.options.get("count", 40))
        headers = {"User-Agent": self.settings.edgar_user_agent}
        items: list[RawItem] = []
        status = None
        for form in forms:
            query = urlencode({"action": "getcurrent", "type": form, "owner": "include",
                               "count": count, "output": "atom"})
            resp = await self._get(f"{EDGAR_CURRENT_URL}?{query}", headers=headers)
            status = resp.status_code
            feed = await asyncio.to_thread(feedparser.parse, resp.content)
            for entry in feed.entries[: self.spec.max_entries]:
                item = self._to_item(entry)
                if item:
                    items.append(item)
        return FetchResult(items=items, http_status=status)

    def _to_item(self, entry) -> RawItem | None:
        title = clean_text(entry.get("title", ""))
        url = (entry.get("link") or "").strip()
        if not title or not url:
            return None
        extra: dict = {}
        m = _TITLE_RE.match(title)
        if m:
            extra = {
                "form": m.group("form"),
                "company": m.group("company"),
                "cik": m.group("cik").lstrip("0"),
                "role": m.group("role") or "",
            }
        published = parse_feed_datetime(entry.get("updated") or entry.get("published"),
                                         entry.get("updated_parsed"), self.spec.naive_tz)
        return RawItem(title=title, url=url, summary=clean_text(entry.get("summary", "")),
                       published_at=published, extra=extra)
