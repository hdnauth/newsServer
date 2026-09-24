from __future__ import annotations

from fastapi import APIRouter, Depends

from newsserver import __version__
from newsserver.api.deps import services
from newsserver.api.v1 import headlines, sources, stats, symbols, topics, watchlists

v1 = APIRouter(prefix="/v1")
for module in (headlines, sources, topics, symbols, watchlists, stats):
    v1.include_router(module.router)

root = APIRouter()


@root.get("/health", tags=["health"])
async def health(svc=Depends(services)) -> dict:
    """``ok`` | ``degraded`` (전 소스 연속 실패·스케줄러 정지). 상태 코드는 항상 200."""
    return {"version": __version__, **svc.scheduler.health()}
