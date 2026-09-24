"""관심종목 — 종목별 소스(per_symbol)의 수집 대상. 클라이언트마다 따로 두고 합집합을 수집한다."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException

from newsserver.api.deps import parse_market, require_token, services
from newsserver.api.schemas import Watchlist, WatchlistPut
from newsserver.markets import normalize_symbol
from newsserver.timeutil import to_iso, utcnow

router = APIRouter(tags=["watchlists"])

_CLIENT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _check_client(client: str) -> str:
    if not _CLIENT_RE.match(client):
        raise HTTPException(422, "client 는 영숫자·_.- 64자 이내")
    return client


@router.get("/watchlists")
async def list_watchlists(svc=Depends(services)) -> dict:
    async with svc.db.read() as conn:
        cur = await conn.execute("SELECT client, COUNT(*) AS n FROM watchlists GROUP BY client ORDER BY client")
        clients = {r["client"]: r["n"] for r in await cur.fetchall()}
    targets = await svc.scheduler.targets()
    return {"clients": clients, "union_size": len(targets)}


@router.get("/watchlists/{client}", response_model=Watchlist)
async def get_watchlist(client: str, svc=Depends(services)) -> dict:
    _check_client(client)
    async with svc.db.read() as conn:
        cur = await conn.execute(
            "SELECT market, symbol, name, aliases_json FROM watchlists WHERE client = ? ORDER BY market, symbol",
            (client,))
        rows = await cur.fetchall()
    return {"client": client, "symbols": [
        {"market": r["market"], "symbol": r["symbol"], "name": r["name"],
         "aliases": json.loads(r["aliases_json"] or "[]")} for r in rows]}


@router.put("/watchlists/{client}", response_model=Watchlist, dependencies=[Depends(require_token)])
async def put_watchlist(client: str, body: WatchlistPut, svc=Depends(services)) -> dict:
    """클라이언트의 관심종목 목록을 통째로 교체한다."""
    _check_client(client)
    now = to_iso(utcnow())
    rows = {}
    for item in body.symbols:
        mkt = parse_market(item.market, required=True)
        if mkt == "GLOBAL":
            raise HTTPException(422, "market 은 KR 또는 US 여야 합니다")
        sym = normalize_symbol(item.symbol, mkt)
        if sym:
            rows[(mkt, sym)] = (client, mkt, sym, item.name.strip(),
                                json.dumps([a for a in item.aliases if a.strip()], ensure_ascii=False), now)
    async with svc.db.write() as conn:
        await conn.execute("DELETE FROM watchlists WHERE client = ?", (client,))
        await conn.executemany(
            "INSERT INTO watchlists(client, market, symbol, name, aliases_json, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            list(rows.values()),
        )
    svc.scheduler.mark_targets_dirty()
    return await get_watchlist(client, svc)


@router.delete("/watchlists/{client}", dependencies=[Depends(require_token)])
async def delete_watchlist(client: str, svc=Depends(services)) -> dict:
    _check_client(client)
    async with svc.db.write() as conn:
        cur = await conn.execute("DELETE FROM watchlists WHERE client = ?", (client,))
    svc.scheduler.mark_targets_dirty()
    return {"deleted": cur.rowcount}
