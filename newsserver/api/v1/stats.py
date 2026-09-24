"""운영 통계 — 수집량·저장 용량 추이 (보관 기간 판단용)."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query

from newsserver.api.deps import services
from newsserver.timeutil import to_iso, utcnow

router = APIRouter(tags=["stats"])


def _file_size(path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


@router.get("/stats")
async def stats(days: int = Query(14, ge=1, le=90), svc=Depends(services)) -> dict:
    db_path = svc.db.path
    sizes = {
        "db_bytes": _file_size(db_path),
        "wal_bytes": _file_size(db_path.with_name(db_path.name + "-wal")),
        "backups_bytes": sum(_file_size(p) for p in svc.settings.backup_dir.glob("news-*.db.gz")),
    }
    since = to_iso(utcnow() - dt.timedelta(days=days))
    async with svc.db.read() as conn:
        cur = await conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts), MIN(collected_at) FROM articles")
        total, oldest_ts, newest_ts, first_collected = await cur.fetchone()
        cur = await conn.execute(
            "SELECT substr(collected_at, 1, 10) AS day, COUNT(*) AS n FROM articles "
            "WHERE collected_at >= ? GROUP BY day ORDER BY day", (since,))
        per_day = [{"day": r["day"], "articles": r["n"]} for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT source_key, COUNT(*) AS n FROM articles WHERE collected_at >= ? GROUP BY source_key ORDER BY n DESC",
            (since,))
        per_source = {r["source_key"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute("PRAGMA page_size")
        page_size = (await cur.fetchone())[0]
        cur = await conn.execute("PRAGMA freelist_count")
        free_pages = (await cur.fetchone())[0]

    bytes_per_article = round(sizes["db_bytes"] / total) if total else None
    # 완결된 날(오늘 제외)의 평균 유입량
    full_days = [d["articles"] for d in per_day[:-1]] if len(per_day) > 1 else []
    avg_per_day = round(sum(full_days) / len(full_days)) if full_days else None
    return {
        "articles": {"total": total, "oldest_ts": oldest_ts, "newest_ts": newest_ts,
                     "first_collected_at": first_collected},
        "storage": {**sizes, "free_bytes": free_pages * page_size, "bytes_per_article": bytes_per_article},
        "intake": {"days": days, "avg_per_day": avg_per_day, "per_day": per_day, "per_source": per_source},
        "clients": dict(svc.client_requests),
    }
