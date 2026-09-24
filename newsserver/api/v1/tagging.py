"""태깅 미리보기 — 임의의 텍스트에 수집 시점 태깅 규칙을 적용해 본다 (저장하지 않음)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from newsserver.api.deps import services

router = APIRouter(tags=["tagging"])


class TagPreviewRequest(BaseModel):
    title: str = Field("", max_length=2000)
    summary: str = Field("", max_length=10000)


@router.post("/tagging/preview")
async def tagging_preview(body: TagPreviewRequest, svc=Depends(services)) -> dict:
    """심볼 사전 태깅(dict·ref)과 주제 규칙 결과. 별칭·제외어를 조정할 때 쓴다."""
    symbols = []
    for market, symbol, method in sorted(svc.directory.tag(body.title, body.summary)):
        entry = svc.directory.get(market, symbol)
        symbols.append({"market": market, "symbol": symbol, "method": method,
                        "name": entry.name if entry else ""})
    topics = [{"key": k, "label": svc.topics.labels.get(k, k)}
              for k in svc.topics.extract(body.title, body.summary)]
    return {"symbols": symbols, "topics": topics}
