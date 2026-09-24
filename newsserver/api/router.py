from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from newsserver import __version__
from newsserver.api.deps import services
from newsserver.api.v1 import headlines, sources, stats, symbols, tagging, topics, watchlists

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

v1 = APIRouter(prefix="/v1")
for module in (headlines, sources, topics, symbols, tagging, watchlists, stats):
    v1.include_router(module.router)

root = APIRouter()


@root.get("/health", tags=["health"])
async def health(svc=Depends(services)) -> dict:
    """``ok`` | ``degraded`` (전 소스 연속 실패·스케줄러 정지). 상태 코드는 항상 200."""
    return {"version": __version__, "auth_required": bool(svc.settings.api_token), **svc.scheduler.health()}


@root.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """운영 콘솔 (단일 정적 페이지)."""
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})
