"""원천 응답의 메모리 캐시 — TTL·항목 수 상한(LRU)·동시 요청 합치기. 디스크에 쓰지 않는다."""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Hashable


class MemoryCache:
    def __init__(self, ttl_sec: float, max_entries: int, missing_ttl_sec: float | None = None):
        self.ttl = ttl_sec
        # '없음' 응답(아직 제출되지 않은 보고서 등)은 곧 생길 수 있어 짧게 둔다
        self.missing_ttl = min(ttl_sec, missing_ttl_sec if missing_ttl_sec is not None else 3600)
        self.max_entries = max_entries
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[Hashable, asyncio.Future] = {}
        self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._data)

    def _get(self, key: Hashable) -> tuple[bool, Any]:
        entry = self._data.get(key)
        if entry is None:
            return False, None
        expires, value = entry
        if expires < time.monotonic():
            del self._data[key]
            return False, None
        self._data.move_to_end(key)
        return True, value

    def _set(self, key: Hashable, value: Any) -> None:
        ttl = self.missing_ttl if value is None else self.ttl
        if ttl <= 0 or self.max_entries <= 0:
            return
        self._data[key] = (time.monotonic() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    async def get_or_load(self, key: Hashable, loader: Callable[[], Awaitable[Any]]) -> Any:
        """캐시에 있으면 그 값, 같은 키를 이미 읽는 중이면 그 결과를 함께 기다린다. 예외는 캐시하지 않는다."""
        found, value = self._get(key)
        if found:
            self.hits += 1
            return value
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        self.misses += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            value = await loader()
        except BaseException as e:
            future.set_exception(e)
            future.exception()  # 기다리는 쪽이 없어도 경고가 남지 않게
            raise
        else:
            self._set(key, value)
            future.set_result(value)
            return value
        finally:
            self._inflight.pop(key, None)

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._data), "hits": self.hits, "misses": self.misses}
