"""재무제표 조회 서비스 — 심볼 사전으로 원천 식별자를 찾아 시장별 제공자에 넘긴다."""
from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from newsserver.config import Settings
from newsserver.financials.cache import MemoryCache
from newsserver.financials.dart import DartFinancials
from newsserver.financials.model import NotFound
from newsserver.symbols import SymbolDirectory
from newsserver.timeutil import to_iso, utcnow


class FinancialsService:
    def __init__(self, settings: Settings, http: httpx.AsyncClient, directory: SymbolDirectory):
        self.settings = settings
        self.directory = directory
        self.cache = MemoryCache(settings.financials_cache_ttl_sec, settings.financials_cache_max_entries)
        self.dart = DartFinancials(settings, http, self.cache)

    async def get(self, market: str, symbol: str, *, period: str = "quarter", limit: int = 8,
                  basis: str = "consolidated", raw: bool = False, today: dt.date | None = None) -> dict[str, Any]:
        entry = self.directory.get(market, symbol)
        if entry is None:
            raise NotFound(f"사전에 없는 종목입니다: {market}:{symbol}")
        if market == "KR":
            if not entry.corp_code:
                raise NotFound(f"DART 고유번호가 없는 종목입니다: {symbol}")
            periods, currency = await self.dart.periods(entry.corp_code, period=period, limit=limit,
                                                        basis=basis, raw=raw, today=today)
            source, source_id = "dart", entry.corp_code
        else:
            raise NotFound(f"재무제표를 지원하지 않는 시장입니다: {market}")
        return {
            "market": market, "symbol": symbol, "name": entry.name, "source": source, "source_id": source_id,
            "currency": currency, "period": period, "basis": basis,
            "periods": [p.to_dict() for p in periods], "fetched_at": to_iso(utcnow()),
        }
