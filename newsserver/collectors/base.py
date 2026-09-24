"""수집기 공통 인터페이스.

수집기는 원격에서 항목을 가져와 ``RawItem`` 으로 돌려주는 데까지만 책임진다.
중복 제거·정규화·태깅·저장은 파이프라인(``pipeline.py``)이 맡는다.
"""
from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

from newsserver.config import Settings
from newsserver.sources import SourceSpec


@dataclass
class SymbolTag:
    market: str
    symbol: str
    method: str  # source_tag | dart_code | edgar_cik | target ...


@dataclass
class RawItem:
    title: str
    url: str
    summary: str = ""
    published_at: dt.datetime | None = None
    symbols: list[SymbolTag] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    items: list[RawItem] = field(default_factory=list)
    http_status: int | None = None
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None


@dataclass
class SymbolTarget:
    market: str
    symbol: str
    names: list[str] = field(default_factory=list)

    @property
    def primary_name(self) -> str:
        return self.names[0] if self.names else self.symbol


class CollectorError(RuntimeError):
    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class Collector(abc.ABC):
    kind: ClassVar[str]

    def __init__(self, spec: SourceSpec, settings: Settings, http: httpx.AsyncClient):
        self.spec = spec
        self.settings = settings
        self.http = http

    def missing_config(self) -> str | None:
        """필요한 설정이 없으면 이유를 돌려준다 (그 소스는 수집하지 않는다)."""
        return None

    async def fetch(self, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        raise NotImplementedError(f"{self.kind} 는 전역 수집을 지원하지 않습니다")

    async def fetch_symbol(self, target: SymbolTarget) -> FetchResult:
        raise NotImplementedError(f"{self.kind} 는 종목별 수집을 지원하지 않습니다")

    async def _get(self, url: str, *, params: dict | None = None, headers: dict | None = None,
                   etag: str | None = None, last_modified: str | None = None) -> httpx.Response:
        req_headers = dict(headers or {})
        if etag:
            req_headers["If-None-Match"] = etag
        if last_modified:
            req_headers["If-Modified-Since"] = last_modified
        resp = await self.http.get(url, params=params, headers=req_headers)
        if resp.status_code not in (200, 304):
            raise CollectorError(f"HTTP {resp.status_code}", resp.status_code)
        return resp
