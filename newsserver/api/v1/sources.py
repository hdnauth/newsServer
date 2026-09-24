"""소스 상태·토글·수동 수집."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query

from newsserver.api.deps import require_token, services
from newsserver.api.schemas import SourcePatch, SourceStatus
from newsserver.scheduler import SourceBusy, SourceState
from newsserver.timeutil import to_iso, utcnow

router = APIRouter(tags=["sources"])


async def _counts(svc) -> tuple[dict[str, int], dict[str, int]]:
    since = to_iso(utcnow() - dt.timedelta(hours=24))
    async with svc.db.read() as conn:
        cur = await conn.execute(
            "SELECT source_key, COUNT(*), SUM(seen_at >= ?) FROM article_feeds GROUP BY source_key", (since,))
        rows = await cur.fetchall()
    return {r[0]: r[1] for r in rows}, {r[0]: r[2] or 0 for r in rows}


def _status(svc, key: str, totals: dict, recent: dict) -> dict:
    spec = svc.specs[key]
    st = svc.scheduler.state.get(key, SourceState())
    issue = svc.scheduler.missing_config(key)
    return {
        "key": key, "kind": spec.kind, "label": spec.label, "market": spec.market, "lang": spec.lang,
        "category": spec.category, "body_kind": spec.body_kind, "is_filing": spec.is_filing,
        "per_symbol": spec.per_symbol, "enabled": svc.scheduler.is_enabled(key),
        "enabled_override": st.enabled_override, "configured": issue is None, "config_issue": issue,
        "last_fetch_at": st.last_fetch_at, "last_success_at": st.last_success_at, "last_new_at": st.last_new_at,
        "last_error": st.last_error, "fail_streak": st.fail_streak, "next_poll_at": st.next_poll_at,
        "articles_24h": recent.get(key, 0), "articles_total": totals.get(key, 0),
    }


@router.get("/sources", response_model=list[SourceStatus])
async def list_sources(svc=Depends(services)) -> list[dict]:
    totals, recent = await _counts(svc)
    return [_status(svc, key, totals, recent) for key in svc.specs]


@router.get("/fetch-log")
async def fetch_log(source: str | None = None, limit: int = Query(50, ge=1, le=500),
                    errors_only: bool = False, svc=Depends(services)) -> list[dict]:
    """최근 수집 이력 (최신순).

    ``maybe_missed``: 받은 건수가 소스의 max_entries 에 닿았고 그 전부가 신규였던 수집 —
    폴링 간격 사이에 상한보다 많이 발행돼 일부를 놓쳤을 수 있다.
    """
    where, params = [], []
    if source:
        where.append("source_key = ?")
        params.append(source)
    if errors_only:
        where.append("error IS NOT NULL")
    sql = ("SELECT source_key, target, started_at, duration_ms, http_status, n_items, n_new, error FROM fetch_log"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?")
    async with svc.db.read() as conn:
        cur = await conn.execute(sql, (*params, limit))
        rows = await cur.fetchall()
    out = []
    for r in rows:
        spec = svc.specs.get(r["source_key"])
        at_cap = bool(spec and r["n_items"] >= spec.max_entries)
        out.append({**dict(r), "maybe_missed": at_cap and r["n_new"] >= r["n_items"]})
    return out


@router.patch("/sources/{key}", response_model=SourceStatus, dependencies=[Depends(require_token)])
async def patch_source(key: str, body: SourcePatch, svc=Depends(services)) -> dict:
    if key not in svc.specs:
        raise HTTPException(404, f"소스 없음: {key}")
    await svc.scheduler.set_enabled(key, body.enabled)
    totals, recent = await _counts(svc)
    return _status(svc, key, totals, recent)


@router.post("/sources/{key}/refresh", dependencies=[Depends(require_token)])
async def refresh_source(key: str, svc=Depends(services)) -> dict:
    spec = svc.specs.get(key)
    if spec is None:
        raise HTTPException(404, f"소스 없음: {key}")
    if spec.per_symbol:
        raise HTTPException(422, "종목별 소스는 GET /v1/headlines?symbol=...&refresh=true 로 갱신합니다")
    issue = svc.scheduler.missing_config(key)
    if issue:
        raise HTTPException(409, f"소스 미설정: {issue}")
    try:
        stats = await svc.scheduler.run_source_now(key)
    except SourceBusy:
        raise HTTPException(409, "이미 수집 중입니다") from None
    if stats is None:
        return {"ok": False, "error": svc.scheduler.state.get(key, SourceState()).last_error}
    return {"ok": True, "received": stats.n_items, "new": stats.n_new, "updated": stats.n_updated}
