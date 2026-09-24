"""NewsServer 클라이언트.

기본 동작은 fail-soft 다: 서버가 죽었거나 느려도 예외 대신 빈 결과를 돌려주고 경고 로그를
남긴다. 뉴스는 대개 보조 입력이라 뉴스 서버 장애가 호출자를 멈추게 하지 않기 위해서다.
예외가 필요하면 ``raise_errors=True``.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

import httpx

from newsclient.models import Headline, HeadlinePage

log = logging.getLogger("newsclient")

DEFAULT_URL = "http://127.0.0.1:5200"


def _params(**kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in kw.items():
        if v is None or v == [] or v == ():
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, (list, tuple, set)) and k in ("name", "alias"):
            out[k] = list(v)  # 반복 파라미터
        elif isinstance(v, (list, tuple, set)):
            out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = v
    return out


def _page(data: dict[str, Any]) -> HeadlinePage:
    return HeadlinePage(items=[Headline.from_json(i) for i in data.get("items") or []],
                        next_since_id=data.get("next_since_id"), query=data.get("query") or {})


class _Base:
    def __init__(self, base_url: str, client: str | None, token: str | None, raise_errors: bool):
        self.base_url = base_url.rstrip("/")
        self.raise_errors = raise_errors
        self.headers = {}
        if client:
            self.headers["X-Client"] = client
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _fail(self, what: str, exc: Exception, empty):
        if self.raise_errors:
            raise exc
        log.warning("NewsServer %s 실패: %s", what, exc)
        return empty


class NewsClient(_Base):
    """비동기 클라이언트.

    >>> async with NewsClient(client="myapp") as nc:
    ...     items = await nc.symbol_headlines("NVDA", "US", hours=24)
    """

    def __init__(self, base_url: str = DEFAULT_URL, *, client: str | None = None, token: str | None = None,
                 timeout: float = 5.0, raise_errors: bool = False, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(base_url, client, token, raise_errors)
        self._http = httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=timeout,
                                       transport=transport)

    async def __aenter__(self) -> "NewsClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw) -> Any:
        resp = await self._http.request(method, path, **kw)
        resp.raise_for_status()
        return resp.json()

    # ── 헤드라인 ────────────────────────────────────────────────────────────
    async def headlines_page(self, **filters: Any) -> HeadlinePage:
        """``GET /v1/headlines`` 의 모든 파라미터를 그대로 받는다 (리스트는 콤마로 합친다)."""
        try:
            return _page(await self._request("GET", "/v1/headlines", params=_params(**filters)))
        except (httpx.HTTPError, ValueError) as e:
            return self._fail("headlines", e, HeadlinePage([], filters.get("since_id"), ok=False, error=str(e)))

    async def headlines(self, **filters: Any) -> list[Headline]:
        return (await self.headlines_page(**filters)).items

    async def symbol_headlines(self, symbol: str, market: str, *, hours: float | None = None, limit: int = 20,
                               names: Sequence[str] = (), refresh: bool = False, **filters: Any) -> list[Headline]:
        """종목 관련 헤드라인. ``names`` 에 회사명·별칭을 주면 사전에 없는 이름도 매칭한다."""
        return await self.headlines(symbol=symbol, market=market, hours=hours, limit=limit,
                                    name=list(names), refresh=refresh, **filters)

    async def topic_headlines(self, topics: Iterable[str], *, hours: float | None = None, limit: int = 50,
                              **filters: Any) -> list[Headline]:
        return await self.headlines(topics=list(topics), hours=hours, limit=limit, **filters)

    async def since(self, cursor: int, *, limit: int = 500, **filters: Any) -> tuple[list[Headline], int]:
        """증분 조회. 반환한 커서를 다음 호출에 넘긴다. 실패 시 커서를 그대로 돌려준다."""
        page = await self.headlines_page(since_id=cursor, limit=limit, **filters)
        return page.items, page.next_since_id if page.next_since_id is not None else cursor

    async def latest_id(self) -> int:
        """현재 최신 기사 id — 증분 소비자의 초기 커서로 쓴다 (과거 기사를 건너뛰고 싶을 때)."""
        page = await self.headlines_page(limit=1)
        if not page.ok and self.raise_errors:
            raise RuntimeError(page.error)
        return page.items[0].id if page.items else 0

    async def by_symbols(self, symbols: Iterable[tuple[str, str] | dict[str, Any]], *, hours: float | None = None,
                         limit_per_symbol: int = 10, **options: Any) -> dict[str, list[Headline]]:
        """여러 종목을 한 번에. 키는 ``"US:NVDA"`` 형식."""
        payload = [s if isinstance(s, dict) else {"symbol": s[0], "market": s[1]} for s in symbols]
        body = {"symbols": payload, "hours": hours, "limit_per_symbol": limit_per_symbol, **options}
        try:
            data = await self._request("POST", "/v1/headlines/by-symbols", json=body)
        except (httpx.HTTPError, ValueError) as e:
            return self._fail("by-symbols", e, {})
        return {k: [Headline.from_json(i) for i in v] for k, v in data["results"].items()}

    async def article(self, article_id: int) -> Headline | None:
        try:
            return Headline.from_json(await self._request("GET", f"/v1/articles/{article_id}"))
        except (httpx.HTTPError, ValueError) as e:
            return self._fail("article", e, None)

    # ── 주제·소스·관심종목 ──────────────────────────────────────────────────
    async def topics_for(self, name: str, symbol: str = "") -> list[str]:
        try:
            data = await self._request("GET", "/v1/topics/for", params={"name": name, "symbol": symbol})
        except (httpx.HTTPError, ValueError) as e:
            return self._fail("topics/for", e, [])
        return [t["key"] for t in data["topics"]]

    async def topic_stats(self, days: float = 30, market: str | None = None) -> dict[str, Any]:
        try:
            return await self._request("GET", "/v1/topics/stats", params=_params(days=days, market=market))
        except (httpx.HTTPError, ValueError) as e:
            return self._fail("topics/stats", e, {})

    async def sources(self) -> list[dict[str, Any]]:
        try:
            return await self._request("GET", "/v1/sources")
        except (httpx.HTTPError, ValueError) as e:
            return self._fail("sources", e, [])

    async def set_source_enabled(self, key: str, enabled: bool | None) -> bool:
        try:
            await self._request("PATCH", f"/v1/sources/{key}", json={"enabled": enabled})
            return True
        except (httpx.HTTPError, ValueError) as e:
            return self._fail("source toggle", e, False)

    async def refresh_source(self, key: str) -> dict[str, Any]:
        try:
            return await self._request("POST", f"/v1/sources/{key}/refresh")
        except (httpx.HTTPError, ValueError) as e:
            return self._fail("source refresh", e, {"ok": False, "error": str(e)})

    async def set_watchlist(self, client: str, symbols: Iterable[tuple[str, str] | dict[str, Any]]) -> bool:
        """관심종목 교체 — 종목별 소스(per_symbol)가 이 목록을 수집한다."""
        payload = [s if isinstance(s, dict) else {"symbol": s[0], "market": s[1]} for s in symbols]
        try:
            await self._request("PUT", f"/v1/watchlists/{client}", json={"symbols": payload})
            return True
        except (httpx.HTTPError, ValueError) as e:
            return self._fail("watchlist", e, False)

    async def health(self) -> dict[str, Any]:
        try:
            return await self._request("GET", "/health")
        except (httpx.HTTPError, ValueError) as e:
            return self._fail("health", e, {"status": "unreachable", "error": str(e)})


class SyncNewsClient(_Base):
    """동기 클라이언트 (주요 조회만). 비동기 버전과 같은 fail-soft 규칙."""

    def __init__(self, base_url: str = DEFAULT_URL, *, client: str | None = None, token: str | None = None,
                 timeout: float = 5.0, raise_errors: bool = False, transport: httpx.BaseTransport | None = None):
        super().__init__(base_url, client, token, raise_errors)
        self._http = httpx.Client(base_url=self.base_url, headers=self.headers, timeout=timeout, transport=transport)

    def __enter__(self) -> "SyncNewsClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def headlines_page(self, **filters: Any) -> HeadlinePage:
        try:
            resp = self._http.get("/v1/headlines", params=_params(**filters))
            resp.raise_for_status()
            return _page(resp.json())
        except (httpx.HTTPError, ValueError) as e:
            return self._fail("headlines", e, HeadlinePage([], filters.get("since_id"), ok=False, error=str(e)))

    def headlines(self, **filters: Any) -> list[Headline]:
        return self.headlines_page(**filters).items

    def symbol_headlines(self, symbol: str, market: str, *, hours: float | None = None, limit: int = 20,
                         names: Sequence[str] = (), refresh: bool = False, **filters: Any) -> list[Headline]:
        return self.headlines(symbol=symbol, market=market, hours=hours, limit=limit, name=list(names),
                              refresh=refresh, **filters)

    def topic_headlines(self, topics: Iterable[str], *, hours: float | None = None, limit: int = 50,
                        **filters: Any) -> list[Headline]:
        return self.headlines(topics=list(topics), hours=hours, limit=limit, **filters)

    def since(self, cursor: int, *, limit: int = 500, **filters: Any) -> tuple[list[Headline], int]:
        page = self.headlines_page(since_id=cursor, limit=limit, **filters)
        return page.items, page.next_since_id if page.next_since_id is not None else cursor
