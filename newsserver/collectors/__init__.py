"""수집기 레지스트리 — sources.yaml 의 ``kind`` 를 수집기 클래스로 연결한다."""
from __future__ import annotations

from newsserver.collectors.base import Collector
from newsserver.collectors.dart import DartCollector
from newsserver.collectors.edgar import EdgarCollector
from newsserver.collectors.rss import RSSCollector, RSSSearchCollector
from newsserver.collectors.yahoo import YahooSymbolCollector

COLLECTORS: dict[str, type[Collector]] = {
    cls.kind: cls
    for cls in (RSSCollector, RSSSearchCollector, DartCollector, EdgarCollector, YahooSymbolCollector)
}

# 종목별 수집(fetch_symbol)을 구현한 kind
PER_SYMBOL_KINDS = {"rss_search", "yahoo_symbol"}
