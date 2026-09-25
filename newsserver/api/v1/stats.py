"""운영 통계 — 수집량·저장 용량 추이 (보관 기간 판단용)."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query

from newsserver.api.deps import services
from newsserver.sources import case_sql, retention_by_source
from newsserver.timeutil import parse_iso, to_iso, utcnow

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
        "projection": await _projection(svc, first_collected, days, total, bytes_per_article),
        "articles": {"total": total, "oldest_ts": oldest_ts, "newest_ts": newest_ts,
                     "first_collected_at": first_collected},
        "storage": {**sizes, "free_bytes": free_pages * page_size, "bytes_per_article": bytes_per_article},
        "intake": {"days": days, "avg_per_day": avg_per_day, "per_day": per_day, "per_source": per_source},
        "clients": dict(svc.client_requests),
    }


# 기사가 적을 때는 페이지·인덱스 고정 비용 때문에 기사당 바이트가 과대 측정된다.
# 그 구간에서는 1년 규모 모의 DB 실측값(docs/DESIGN.md §8)을 쓴다.
REFERENCE_BYTES_PER_ARTICLE = 1700
MEASURED_MIN_ARTICLES = 50_000
BACKUP_COMPRESSION = 0.35
WARMUP = dt.timedelta(hours=6)
MIN_OBSERVED = dt.timedelta(days=1)


async def _projection(svc, first_collected: str | None, days: int, total: int,
                      bytes_per_article: int | None) -> dict:
    """현재 유입 속도가 이어질 때 보관 기간이 다 찼을 때의 기사 수·용량 추정.

    첫 수집 직후에는 각 피드가 쌓아 둔 과거 기사를 한꺼번에 받으므로 유입 속도가 부풀려진다.
    첫 수집 후 ``WARMUP`` 동안은 관측에서 빼고, 관측이 ``MIN_OBSERVED`` 이상일 때만 추정한다.
    """
    first = parse_iso(first_collected)
    if first is None:
        return {"ready": False, "reason": "수집된 기사 없음"}
    now = utcnow()
    since = max(first + WARMUP, now - dt.timedelta(days=days))
    span = now - since
    if span < MIN_OBSERVED:
        ready_at = first + WARMUP + MIN_OBSERVED
        return {"ready": False, "reason": "관측 기간 부족", "ready_at": to_iso(ready_at)}

    # 기사는 실린 피드들의 보관 기간 중 가장 긴 것을 따른다 (보관 정리와 같은 기준)
    default_days = svc.settings.default_retention_days
    days_expr, params = case_sql("COALESCE(f.source_key, a.source_key)",
                                 retention_by_source(svc.specs.values(), default_days), default_days)
    async with svc.db.read() as conn:
        cur = await conn.execute(
            f"SELECT days, COUNT(*) FROM (SELECT MAX({days_expr}) AS days FROM articles a "
            f"LEFT JOIN article_feeds f ON f.article_id = a.id WHERE a.collected_at >= ? GROUP BY a.id) "
            f"GROUP BY days",
            (*params, to_iso(since)))
        per_retention = {r[0]: r[1] for r in await cur.fetchall()}
    span_days = span.total_seconds() / 86400
    projected = sum(n / span_days * days for days, n in per_retention.items())
    measured = total >= MEASURED_MIN_ARTICLES and bytes_per_article
    bpa = bytes_per_article if measured else REFERENCE_BYTES_PER_ARTICLE
    db_bytes = projected * bpa
    return {
        "ready": True,
        "observed_since": to_iso(since),
        "observed_days": round(span_days, 2),
        "articles_per_day": round(sum(per_retention.values()) / span_days),
        "default_retention_days": svc.settings.default_retention_days,
        "projected_articles": round(projected),
        "bytes_per_article": bpa,
        "bytes_basis": "measured" if measured else "reference",
        "projected_db_bytes": round(db_bytes),
        "projected_backups_bytes": round(db_bytes * BACKUP_COMPRESSION * max(svc.settings.backup_keep, 0)),
    }
