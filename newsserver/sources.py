"""소스 레지스트리 — ``config/sources.yaml`` 로더."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from newsserver.markets import MARKETS

BODY_KINDS = ("summary", "title_only", "metadata")
SCHEDULES = ("market_aware", "fixed")

DEFAULT_MARKET_INTERVALS = {"open": 180, "extended": 1800, "closed": 3600}


@dataclass
class SourceSpec:
    key: str
    kind: str
    label: str
    market: str
    lang: str
    category: str = ""
    body_kind: str = "summary"
    is_filing: bool = False
    enabled: bool = True
    url: str = ""
    retention_days: int | None = None
    schedule: str = "market_aware"
    interval_sec: int = 1800
    market_intervals: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MARKET_INTERVALS))
    max_entries: int = 30
    # 타임존 표기가 없는 발행 시각을 해석할 시간대
    naive_tz: str = "UTC"
    # 종목별 수집 소스: 관심종목(watchlist) 각각에 대해 호출한다
    per_symbol: bool = False
    symbol_markets: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def load_sources(path: Path) -> list[SourceSpec]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults: dict[str, Any] = dict(data.get("defaults") or {})
    specs: list[SourceSpec] = []
    seen: set[str] = set()

    for raw in data.get("sources") or []:
        item = {**defaults, **raw}
        key = str(item.get("key") or "").strip()
        if not key:
            raise ValueError("sources.yaml: key 가 없는 항목이 있습니다")
        if key in seen:
            raise ValueError(f"sources.yaml: 중복 key {key}")
        seen.add(key)

        market = str(item.get("market") or "").upper()
        if market not in MARKETS:
            raise ValueError(f"sources.yaml[{key}]: market 은 {MARKETS} 중 하나여야 합니다")
        body_kind = str(item.get("body_kind") or "summary")
        if body_kind not in BODY_KINDS:
            raise ValueError(f"sources.yaml[{key}]: body_kind 는 {BODY_KINDS} 중 하나여야 합니다")
        schedule = str(item.get("schedule") or "market_aware")
        if schedule not in SCHEDULES:
            raise ValueError(f"sources.yaml[{key}]: schedule 은 {SCHEDULES} 중 하나여야 합니다")

        intervals = dict(DEFAULT_MARKET_INTERVALS)
        intervals.update({k: int(v) for k, v in (item.get("market_intervals") or {}).items()})
        symbol_markets = [m.upper() for m in _as_list(item.get("symbol_markets"))]

        specs.append(SourceSpec(
            key=key,
            kind=str(item["kind"]),
            label=str(item.get("label") or key),
            market=market,
            lang=str(item.get("lang") or "en"),
            category=str(item.get("category") or ""),
            body_kind=body_kind,
            is_filing=bool(item.get("is_filing", False)),
            enabled=bool(item.get("enabled", True)),
            url=str(item.get("url") or ""),
            retention_days=int(item["retention_days"]) if item.get("retention_days") else None,
            schedule=schedule,
            interval_sec=int(item.get("interval_sec") or 1800),
            market_intervals=intervals,
            max_entries=int(item.get("max_entries") or 30),
            naive_tz=str(item.get("naive_tz") or "UTC"),
            per_symbol=bool(item.get("per_symbol", False)),
            symbol_markets=symbol_markets,
            options=dict(item.get("options") or {}),
        ))
    return specs
