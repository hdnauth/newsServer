"""심볼 사전 조회·별칭 관리."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query

from newsserver.api.deps import parse_market, require_token, services
from newsserver.api.schemas import AliasesPut, SymbolInfo
from newsserver.markets import normalize_symbol
from newsserver.timeutil import to_iso, utcnow

router = APIRouter(tags=["symbols"])


def _row(r) -> dict:
    return {"market": r["market"], "symbol": r["symbol"], "name": r["name"], "name_en": r["name_en"],
            "aliases": json.loads(r["aliases_json"] or "[]"), "cik": r["cik"], "origin": r["origin"]}


@router.get("/symbols/search", response_model=list[SymbolInfo])
async def search_symbols(q: str = Query(..., min_length=1), market: str | None = None,
                         limit: int = Query(20, ge=1, le=100), svc=Depends(services)) -> list[dict]:
    """코드·이름·별칭 부분 일치 검색."""
    mkt = parse_market(market)
    like = f"%{q.strip()}%"
    sql = ("SELECT * FROM symbols WHERE (symbol LIKE ? OR name LIKE ? OR name_en LIKE ? OR aliases_json LIKE ?)"
           + (" AND market = ?" if mkt else "") + " ORDER BY (symbol = ?) DESC, length(name) LIMIT ?")
    params = [like, like, like, like, *([mkt] if mkt else []), q.strip().upper(), limit]
    async with svc.db.read() as conn:
        cur = await conn.execute(sql, params)
        return [_row(r) for r in await cur.fetchall()]


@router.get("/symbols/{market}/{symbol}", response_model=SymbolInfo)
async def get_symbol(market: str, symbol: str, svc=Depends(services)) -> dict:
    mkt = parse_market(market, required=True)
    async with svc.db.read() as conn:
        cur = await conn.execute("SELECT * FROM symbols WHERE market = ? AND symbol = ?",
                                 (mkt, normalize_symbol(symbol, mkt)))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, "사전에 없는 종목입니다")
    return _row(row)


@router.put("/symbols/{market}/{symbol}/aliases", response_model=SymbolInfo,
            dependencies=[Depends(require_token)])
async def put_aliases(market: str, symbol: str, body: AliasesPut, svc=Depends(services)) -> dict:
    """별칭 목록을 교체한다. 사전에 없는 종목은 name 과 함께 새로 등록한다.

    새 별칭은 이후 수집되는 기사의 사전 태깅과 모든 조회의 텍스트 매칭에 즉시 쓰인다.
    """
    mkt = parse_market(market, required=True)
    if mkt == "GLOBAL":
        raise HTTPException(422, "market 은 KR 또는 US 여야 합니다")
    sym = normalize_symbol(symbol, mkt)
    aliases = list(dict.fromkeys(a.strip() for a in body.aliases if a.strip()))
    now = to_iso(utcnow())
    async with svc.db.write() as conn:
        cur = await conn.execute("SELECT 1 FROM symbols WHERE market = ? AND symbol = ?", (mkt, sym))
        if await cur.fetchone() is None:
            if not body.name:
                raise HTTPException(404, "사전에 없는 종목입니다 — name 을 함께 보내면 새로 등록합니다")
            await conn.execute(
                "INSERT INTO symbols(market, symbol, name, aliases_json, origin, updated_at) "
                "VALUES (?, ?, ?, ?, 'manual', ?)",
                (mkt, sym, body.name, json.dumps(aliases, ensure_ascii=False), now),
            )
        else:
            await conn.execute("UPDATE symbols SET aliases_json = ? WHERE market = ? AND symbol = ?",
                               (json.dumps(aliases, ensure_ascii=False), mkt, sym))
    await svc.directory.load(svc.db)
    svc.scheduler.mark_targets_dirty()
    return await get_symbol(mkt, sym, svc)


@router.post("/symbols/refresh", dependencies=[Depends(require_token)])
async def refresh_symbols(svc=Depends(services)) -> dict:
    """원격 사전(SEC·DART)을 백그라운드로 다시 내려받는다."""
    svc.scheduler.spawn(svc.scheduler.refresh_symbols(), name="refresh-symbols")
    return {"ok": True, "started": True}
