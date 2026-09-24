"""수집 스케줄러.

* 전역 소스: 소스마다 ``next_poll_at`` 을 두고 도래한 것만 실행한다.
  주기 = max(기본 주기, 무수집 백오프, 실패 백오프). 기본 주기는 ``fixed`` 면 interval_sec,
  ``market_aware`` 면 해당 시장의 정규장/확장 거래/휴장 여부에 따라 달라진다.
* 종목별 소스: 관심종목(watchlist) 합집합을 대상으로 소스마다 한 번에 한 종목씩 순회한다.
  요청 간격(request_gap_sec)은 즉시 수집(refresh)까지 포함해 소스 단위로 지킨다 — 소비자가
  여러 종목을 연달아 즉시 수집해도 원격에는 간격을 두고 한 건씩 나간다. 같은 종목의 수집이
  진행 중이면 새로 요청하지 않고 그 결과를 함께 기다린다.
* 유지보수: 매일 정해진 시각에 보관 기간 정리·수집 이력 정리·백업, 주기적으로 심볼 사전 갱신.
* 상태 감시: 전 소스 연속 실패·스케줄러 정지를 ``degraded`` 로 보고하고 전환 시 경보.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import math
import json
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from newsserver.alerts import Notifier
from newsserver.collectors import COLLECTORS, PER_SYMBOL_KINDS
from newsserver.collectors.base import Collector, CollectorError, FetchResult, SymbolTarget
from newsserver.config import Settings
from newsserver.markets import market_session
from newsserver.pipeline import IngestStats, Ingestor
from newsserver.sources import SourceSpec
from newsserver.storage.db import Database
from newsserver.symbols import SymbolDirectory
from newsserver.timeutil import parse_iso, to_iso, utcnow
from newsserver.topics import TopicRules

TICK_SEC = 1.0
MAX_FAIL_BACKOFF_SEC = 1800
STALE_AFTER = dt.timedelta(hours=6)


def idle_backoff_sec(idle_streak: int) -> int:
    """새 기사가 연속으로 없을 때 폴링 간격을 늘린다."""
    if idle_streak >= 20:
        return 1800
    if idle_streak >= 10:
        return 900
    if idle_streak >= 5:
        return 600
    return 0


def fail_backoff_sec(fail_streak: int) -> int:
    if fail_streak <= 0:
        return 0
    return min(MAX_FAIL_BACKOFF_SEC, 60 * 2 ** (fail_streak - 1))


def base_interval_sec(spec: SourceSpec, now: dt.datetime) -> int:
    if spec.schedule == "fixed":
        return spec.interval_sec
    return int(spec.market_intervals[str(market_session(spec.market, now))])


class SourceBusy(RuntimeError):
    """이미 수집 중인 소스."""


@dataclass
class SourceState:
    enabled_override: bool | None = None
    etag: str | None = None
    last_modified: str | None = None
    last_fetch_at: str | None = None
    last_success_at: str | None = None
    last_new_at: str | None = None
    last_error: str | None = None
    fail_streak: int = 0
    idle_streak: int = 0
    next_poll_at: str | None = None


@dataclass
class SymbolState:
    last_fetch_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    fail_streak: int = 0
    next_poll_at: str | None = None


class Scheduler:
    def __init__(self, *, db: Database, settings: Settings, specs: list[SourceSpec], ingestor: Ingestor,
                 directory: SymbolDirectory, topics: TopicRules, http: httpx.AsyncClient, notifier: Notifier):
        self.db = db
        self.settings = settings
        self.specs = {s.key: s for s in specs}
        self.ingestor = ingestor
        self.directory = directory
        self.topics = topics
        self.http = http
        self.notifier = notifier

        self.collectors: dict[str, Collector] = {}
        for spec in specs:
            cls = COLLECTORS.get(spec.kind)
            if cls is None:
                raise ValueError(f"sources.yaml[{spec.key}]: 알 수 없는 kind '{spec.kind}'")
            if spec.per_symbol != (spec.kind in PER_SYMBOL_KINDS):
                raise ValueError(f"sources.yaml[{spec.key}]: kind '{spec.kind}' 는 "
                                 f"per_symbol: {str(spec.kind in PER_SYMBOL_KINDS).lower()} 이어야 합니다")
            self.collectors[spec.key] = cls(spec, settings, http)

        self.state: dict[str, SourceState] = {}
        self.symbol_state: dict[tuple[str, str, str], SymbolState] = {}
        self._running: set[str] = set()
        self._sem = asyncio.Semaphore(settings.max_concurrent_fetches)
        self._symbol_gates: dict[str, asyncio.Lock] = {}
        self._symbol_last_request: dict[str, float] = {}
        self._symbol_inflight: dict[tuple[str, str, str], asyncio.Task] = {}
        self._tasks: list[asyncio.Task] = []
        self._last_tick: float = 0.0
        self._targets: list[SymbolTarget] = []
        self._targets_dirty = True
        self._health_status = "ok"
        self._background: set[asyncio.Task] = set()

    # ── 수명 주기 ────────────────────────────────────────────────────────────
    async def init_state(self) -> None:
        """sources.yaml 을 DB 에 반영하고 런타임 상태를 메모리로 올린다."""
        async with self.db.write() as conn:
            await conn.execute("UPDATE sources SET active = 0")
            for s in self.specs.values():
                await conn.execute(
                    "INSERT INTO sources(key, kind, label, market, lang, category, body_kind, is_filing, per_symbol, active) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
                    "ON CONFLICT(key) DO UPDATE SET kind = excluded.kind, label = excluded.label, "
                    "market = excluded.market, lang = excluded.lang, category = excluded.category, "
                    "body_kind = excluded.body_kind, is_filing = excluded.is_filing, "
                    "per_symbol = excluded.per_symbol, active = 1",
                    (s.key, s.kind, s.label, s.market, s.lang, s.category, s.body_kind,
                     int(s.is_filing), int(s.per_symbol)),
                )
        async with self.db.read() as conn:
            cur = await conn.execute("SELECT * FROM sources WHERE active = 1")
            for r in await cur.fetchall():
                self.state[r["key"]] = SourceState(
                    enabled_override=None if r["enabled_override"] is None else bool(r["enabled_override"]),
                    etag=r["etag"], last_modified=r["last_modified"], last_fetch_at=r["last_fetch_at"],
                    last_success_at=r["last_success_at"], last_new_at=r["last_new_at"],
                    last_error=r["last_error"], fail_streak=r["fail_streak"], idle_streak=r["idle_streak"],
                    next_poll_at=r["next_poll_at"],
                )
            cur = await conn.execute("SELECT * FROM symbol_fetch_state")
            for r in await cur.fetchall():
                self.symbol_state[(r["source_key"], r["market"], r["symbol"])] = SymbolState(
                    last_fetch_at=r["last_fetch_at"], last_success_at=r["last_success_at"],
                    last_error=r["last_error"], fail_streak=r["fail_streak"], next_poll_at=r["next_poll_at"],
                )

    def start(self) -> None:
        self._last_tick = time.monotonic()
        self._tasks.append(asyncio.create_task(self._global_loop(), name="global-loop"))
        for spec in self.specs.values():
            if spec.per_symbol:
                self._tasks.append(asyncio.create_task(self._symbol_loop(spec), name=f"symbol-loop:{spec.key}"))
        self._tasks.append(asyncio.create_task(self._maintenance_loop(), name="maintenance-loop"))

    async def stop(self) -> None:
        for task in [*self._tasks, *self._background]:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._background, return_exceptions=True)
        self._tasks.clear()

    def spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ── 소스 상태 ────────────────────────────────────────────────────────────
    def is_enabled(self, key: str) -> bool:
        spec = self.specs[key]
        override = self.state.get(key, SourceState()).enabled_override
        return spec.enabled if override is None else override

    async def set_enabled(self, key: str, enabled: bool | None) -> None:
        self.state.setdefault(key, SourceState()).enabled_override = enabled
        async with self.db.write() as conn:
            await conn.execute("UPDATE sources SET enabled_override = ? WHERE key = ?",
                               (None if enabled is None else int(enabled), key))

    def missing_config(self, key: str) -> str | None:
        return self.collectors[key].missing_config()

    async def _save_state(self, key: str, st: SourceState) -> None:
        async with self.db.write() as conn:
            await conn.execute(
                "UPDATE sources SET etag = ?, last_modified = ?, last_fetch_at = ?, last_success_at = ?, "
                "last_new_at = ?, last_error = ?, fail_streak = ?, idle_streak = ?, next_poll_at = ? WHERE key = ?",
                (st.etag, st.last_modified, st.last_fetch_at, st.last_success_at, st.last_new_at,
                 st.last_error, st.fail_streak, st.idle_streak, st.next_poll_at, key),
            )

    async def _log_fetch(self, key: str, target: str, started: dt.datetime, duration_ms: int,
                         result: FetchResult | None, stats: IngestStats | None, error: str | None,
                         http_status: int | None) -> None:
        async with self.db.write() as conn:
            await conn.execute(
                "INSERT INTO fetch_log(source_key, target, started_at, duration_ms, http_status, n_items, n_new, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, target, to_iso(started), duration_ms, http_status,
                 stats.n_items if stats else 0, stats.n_new if stats else 0, error),
            )

    # ── 전역 소스 ────────────────────────────────────────────────────────────
    async def _global_loop(self) -> None:
        while True:
            self._last_tick = time.monotonic()
            try:
                now = utcnow()
                for key, spec in self.specs.items():
                    if spec.per_symbol or key in self._running or not self.is_enabled(key):
                        continue
                    if self.missing_config(key):
                        continue
                    due = parse_iso(self.state.setdefault(key, SourceState()).next_poll_at)
                    if due is None or due <= now:
                        self._running.add(key)
                        self.spawn(self._run_source(spec), name=f"fetch:{key}")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 틱 하나의 오류로 스케줄러가 멈추지 않게
                logger.exception("스케줄러 틱 오류: {}", e)
            await asyncio.sleep(TICK_SEC)

    async def run_source_now(self, key: str) -> IngestStats | None:
        """즉시 수집 (API 수동 갱신). 실패하면 None — 원인은 ``state[key].last_error``."""
        if key in self._running:
            raise SourceBusy(key)
        self._running.add(key)
        return await self._run_source(self.specs[key])

    async def _run_source(self, spec: SourceSpec) -> IngestStats | None:
        key = spec.key
        st = self.state.setdefault(key, SourceState())
        started = utcnow()
        t0 = time.monotonic()
        result: FetchResult | None = None
        stats: IngestStats | None = None
        error: str | None = None
        status: int | None = None
        try:
            async with self._sem:
                result = await self.collectors[key].fetch(st.etag, st.last_modified)
            status = result.http_status
            stats = await self.ingestor.ingest(spec, result.items)
        except asyncio.CancelledError:
            self._running.discard(key)
            raise
        except Exception as e:  # noqa: BLE001 — 소스 하나의 예외가 루프를 멈추지 않게
            error = f"{type(e).__name__}: {e}"[:300]
            status = e.http_status if isinstance(e, CollectorError) else None
        finally:
            self._running.discard(key)

        now = utcnow()
        st.last_fetch_at = to_iso(now)
        if error is None and result is not None:
            st.fail_streak = 0
            st.last_error = None
            st.last_success_at = st.last_fetch_at
            if not result.not_modified:
                st.etag, st.last_modified = result.etag, result.last_modified
            if stats and stats.n_new:
                st.idle_streak = 0
                st.last_new_at = st.last_fetch_at
                logger.info("{} — 신규 {}건 (수신 {}건)", key, stats.n_new, stats.n_items)
            else:
                st.idle_streak += 1
        else:
            st.fail_streak += 1
            st.last_error = error
            logger.warning("{} 수집 실패 ({}회 연속): {}", key, st.fail_streak, error)

        wait = max(base_interval_sec(spec, now), idle_backoff_sec(st.idle_streak), fail_backoff_sec(st.fail_streak))
        st.next_poll_at = to_iso(now + dt.timedelta(seconds=wait))
        await self._save_state(key, st)
        await self._log_fetch(key, "", started, int((time.monotonic() - t0) * 1000), result, stats, error, status)
        return stats

    # ── 종목별 소스 ──────────────────────────────────────────────────────────
    def mark_targets_dirty(self) -> None:
        self._targets_dirty = True

    async def _load_targets(self) -> list[SymbolTarget]:
        async with self.db.read() as conn:
            cur = await conn.execute("SELECT market, symbol, name, aliases_json FROM watchlists")
            rows = await cur.fetchall()
        merged: dict[tuple[str, str], list[str]] = {}
        for r in rows:
            names = merged.setdefault((r["market"], r["symbol"]), [])
            names += [n for n in [r["name"], *json.loads(r["aliases_json"] or "[]")] if n]
        targets = []
        for (market, symbol), names in sorted(merged.items()):
            all_names = list(dict.fromkeys([*names, *self.directory.names_for(market, symbol)]))
            targets.append(SymbolTarget(market, symbol, all_names))
        return targets

    async def targets(self) -> list[SymbolTarget]:
        if self._targets_dirty:
            self._targets = await self._load_targets()
            self._targets_dirty = False
        return self._targets

    @staticmethod
    def _applies(spec: SourceSpec, market: str) -> bool:
        return not spec.symbol_markets or market in spec.symbol_markets

    async def _symbol_loop(self, spec: SourceSpec) -> None:
        while True:
            try:
                if not self.is_enabled(spec.key) or self.missing_config(spec.key):
                    await asyncio.sleep(30)
                    continue
                now = utcnow()
                due: SymbolTarget | None = None
                due_at: dt.datetime | None = None
                for target in await self.targets():
                    if not self._applies(spec, target.market):
                        continue
                    st = self.symbol_state.get((spec.key, target.market, target.symbol))
                    at = parse_iso(st.next_poll_at) if st else None
                    if at is None:
                        due, due_at = target, None
                        break
                    if due_at is None or at < due_at:
                        due, due_at = target, at
                if due is not None and (due_at is None or due_at <= now):
                    await self._symbol_job(spec, due)  # 간격은 _paced 가 지킨다
                else:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("종목별 수집 루프 오류 ({}): {}", spec.key, e)
                await asyncio.sleep(30)

    @asynccontextmanager
    async def _paced(self, spec: SourceSpec):
        """종목별 소스의 원격 요청을 소스마다 한 줄로 세우고 request_gap_sec 간격을 둔다."""
        gap = float(spec.options.get("request_gap_sec", 2.0))
        async with self._symbol_gates.setdefault(spec.key, asyncio.Lock()):
            wait = self._symbol_last_request.get(spec.key, -math.inf) + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                yield
            finally:
                self._symbol_last_request[spec.key] = time.monotonic()

    def _symbol_job(self, spec: SourceSpec, target: SymbolTarget) -> asyncio.Task:
        """종목 수집 작업. 같은 소스·종목이 이미 수집 중이면 그 작업을 돌려준다."""
        key = (spec.key, target.market, target.symbol)
        task = self._symbol_inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._run_symbol(spec, target),
                                       name=f"fetch:{spec.key}:{target.market}:{target.symbol}")
            self._symbol_inflight[key] = task
            task.add_done_callback(
                lambda t: self._symbol_inflight.pop(key) if self._symbol_inflight.get(key) is t else None)
        return task

    async def _run_symbol(self, spec: SourceSpec, target: SymbolTarget) -> IngestStats | None:
        state_key = (spec.key, target.market, target.symbol)
        st = self.symbol_state.setdefault(state_key, SymbolState())
        started = utcnow()
        t0 = time.monotonic()
        result = stats = None
        error = None
        status = None
        try:
            async with self._paced(spec):
                started, t0 = utcnow(), time.monotonic()  # 대기 시간은 수집 시간에서 뺀다
                async with self._sem:
                    result = await self.collectors[spec.key].fetch_symbol(target)
            status = result.http_status
            stats = await self.ingestor.ingest(spec, result.items, target=target)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"[:300]
            status = e.http_status if isinstance(e, CollectorError) else None

        now = utcnow()
        st.last_fetch_at = to_iso(now)
        if error is None:
            st.fail_streak, st.last_error, st.last_success_at = 0, None, st.last_fetch_at
            if stats and stats.n_new:
                logger.info("{} [{}:{}] — 신규 {}건", spec.key, target.market, target.symbol, stats.n_new)
        else:
            st.fail_streak += 1
            st.last_error = error
            logger.warning("{} [{}:{}] 수집 실패: {}", spec.key, target.market, target.symbol, error)
        wait = max(spec.interval_sec, fail_backoff_sec(st.fail_streak))
        st.next_poll_at = to_iso(now + dt.timedelta(seconds=wait))
        async with self.db.write() as conn:
            await conn.execute(
                "INSERT INTO symbol_fetch_state(source_key, market, symbol, last_fetch_at, last_success_at, "
                "last_error, fail_streak, next_poll_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_key, market, symbol) DO UPDATE SET last_fetch_at = excluded.last_fetch_at, "
                "last_success_at = excluded.last_success_at, last_error = excluded.last_error, "
                "fail_streak = excluded.fail_streak, next_poll_at = excluded.next_poll_at",
                (*state_key, st.last_fetch_at, st.last_success_at, st.last_error, st.fail_streak, st.next_poll_at),
            )
        await self._log_fetch(spec.key, f"{target.market}:{target.symbol}", started,
                              int((time.monotonic() - t0) * 1000), result, stats, error, status)
        return stats

    async def refresh_symbol(self, market: str, symbol: str, names: list[str], timeout: float = 15.0) -> dict[str, str]:
        """종목별 소스를 즉시 수집한다. 소스·종목마다 쿨다운을 둔다. 반환: {source_key: 결과}"""
        target = SymbolTarget(market, symbol,
                              list(dict.fromkeys([*names, *self.directory.names_for(market, symbol)])))
        now = utcnow()
        jobs: dict[str, asyncio.Task] = {}
        outcome: dict[str, str] = {}
        for spec in self.specs.values():
            if not spec.per_symbol or not self._applies(spec, market) or not self.is_enabled(spec.key):
                continue
            if self.missing_config(spec.key):
                continue
            st = self.symbol_state.get((spec.key, market, symbol))
            last = parse_iso(st.last_fetch_at) if st else None
            if last and (now - last).total_seconds() < self.settings.refresh_cooldown_sec:
                outcome[spec.key] = "cooldown"
                continue
            jobs[spec.key] = self._symbol_job(spec, target)
        if jobs:
            done, pending = await asyncio.wait(jobs.values(), timeout=timeout)
            for key, task in jobs.items():
                if task in pending:
                    outcome[key] = "timeout"  # 백그라운드에서 계속 진행된다
                    self._background.add(task)
                    task.add_done_callback(self._background.discard)
                else:
                    stats = task.result()
                    outcome[key] = f"ok:{stats.n_new}" if stats else "error"
        return outcome

    # ── 상태 ────────────────────────────────────────────────────────────────
    def scheduler_alive(self) -> bool:
        return (time.monotonic() - self._last_tick) < 30

    def health(self) -> dict:
        now = utcnow()
        active = [k for k, s in self.specs.items()
                  if not s.per_symbol and self.is_enabled(k) and not self.missing_config(k)]
        failing = [k for k in active if self.state.get(k, SourceState()).fail_streak >= 2]
        stale = []
        for k in active:
            last = parse_iso(self.state.get(k, SourceState()).last_success_at)
            if last is None or now - last > STALE_AFTER:
                stale.append(k)
        problems = []
        if self.settings.scheduler_enabled and not self.scheduler_alive():
            problems.append("스케줄러 응답 없음")
        if active and len(failing) == len(active):
            problems.append(f"전 소스 연속 실패 ({len(failing)}/{len(active)})")
        return {
            "status": "degraded" if problems else "ok",
            "problems": problems,
            "sources_active": len(active),
            "sources_failing": failing,
            "sources_stale": stale,
        }

    # ── 유지보수 ────────────────────────────────────────────────────────────
    async def _maintenance_loop(self) -> None:
        await asyncio.sleep(5)
        await self._startup_jobs()
        while True:
            try:
                await self._check_health_transition()
                await self._daily_jobs()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("유지보수 작업 오류: {}", e)
            await asyncio.sleep(60)

    async def _startup_jobs(self) -> None:
        try:
            if await self._symbols_due():
                await self.refresh_symbols()
            if await self.db.get_meta("topics_version") != str(self.topics.version):
                logger.info("주제 규칙 버전 변경 → 재태깅")
                await self.ingestor.retag(topics=True, symbols=False)
                await self.db.set_meta("topics_version", str(self.topics.version))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("시작 작업 오류: {}", e)

    async def _symbols_due(self) -> bool:
        last = parse_iso(await self.db.get_meta("symbols_refreshed_at"))
        return last is None or utcnow() - last > dt.timedelta(days=self.settings.symbol_refresh_days)

    async def refresh_symbols(self) -> dict[str, int]:
        stats = await self.directory.refresh_remote(
            self.db, self.http, edgar_user_agent=self.settings.edgar_user_agent,
            dart_api_key=self.settings.dart_api_key,
        )
        await self.directory.apply_manual_aliases(self.db, self.settings.aliases_file)
        await self.directory.load(self.db)
        self.mark_targets_dirty()
        if stats:
            await self.db.set_meta("symbols_refreshed_at", to_iso(utcnow()))
            # 사전이 바뀌었으니 보관 중인 기사의 사전 태그를 다시 계산한다
            await self.ingestor.retag(topics=False, symbols=True)
        return stats

    async def _check_health_transition(self) -> None:
        health = self.health()
        if health["status"] != self._health_status:
            if health["status"] == "degraded":
                await self.notifier.send("상태 이상: " + ", ".join(health["problems"]))
            else:
                await self.notifier.send("정상 복구")
            self._health_status = health["status"]

    async def _daily_jobs(self) -> None:
        local_now = dt.datetime.now(ZoneInfo(self.settings.local_tz))
        if local_now.hour != self.settings.maintenance_hour:
            return
        today = local_now.date().isoformat()
        if await self.db.get_meta("maintenance_date") == today:
            return
        await self.db.set_meta("maintenance_date", today)
        logger.info("일일 유지보수 시작")
        deleted = await self.purge_expired()
        await self._prune_symbol_state()
        if local_now.weekday() == 6:
            async with self.db.write() as conn:
                await conn.execute("PRAGMA incremental_vacuum")
            async with self.db.write() as conn:
                await conn.execute("INSERT INTO articles_fts(articles_fts) VALUES ('optimize')")
        await self.backup(today)
        if await self._symbols_due():
            await self.refresh_symbols()
        logger.info("일일 유지보수 완료 — 삭제 {}", deleted)

    async def purge_expired(self, batch: int = 5000) -> dict[str, int]:
        """소스별 보관 기간이 지난 기사를 삭제한다 (배치 단위로 쓰기 락을 짧게 잡는다)."""
        now = utcnow()
        async with self.db.read() as conn:
            cur = await conn.execute("SELECT DISTINCT source_key FROM articles")
            keys = [r[0] for r in await cur.fetchall()]
        deleted: dict[str, int] = {}
        for key in keys:
            spec = self.specs.get(key)
            days = (spec.retention_days if spec and spec.retention_days else self.settings.default_retention_days)
            cutoff = to_iso(now - dt.timedelta(days=days))
            total = 0
            while True:
                async with self.db.write() as conn:
                    cur = await conn.execute(
                        "DELETE FROM articles WHERE id IN "
                        "(SELECT id FROM articles WHERE source_key = ? AND ts < ? LIMIT ?)",
                        (key, cutoff, batch),
                    )
                    n = cur.rowcount or 0
                total += n
                if n < batch:
                    break
                await asyncio.sleep(0)
            if total:
                deleted[key] = total
        log_cutoff = to_iso(now - dt.timedelta(days=self.settings.fetch_log_retention_days))
        async with self.db.write() as conn:
            cur = await conn.execute("DELETE FROM fetch_log WHERE started_at < ?", (log_cutoff,))
            if cur.rowcount:
                deleted["fetch_log"] = cur.rowcount
        return deleted

    async def _prune_symbol_state(self) -> None:
        targets = {(t.market, t.symbol) for t in await self._load_targets()}
        stale = [k for k in self.symbol_state if (k[1], k[2]) not in targets]
        if not stale:
            return
        async with self.db.write() as conn:
            await conn.executemany(
                "DELETE FROM symbol_fetch_state WHERE source_key = ? AND market = ? AND symbol = ?", stale)
        for k in stale:
            self.symbol_state.pop(k, None)

    async def backup(self, tag: str) -> None:
        keep = self.settings.backup_keep
        if keep <= 0:
            return
        backup_dir = self.settings.backup_dir
        raw = backup_dir / f"news-{tag}.db"
        target = backup_dir / f"news-{tag}.db.gz"
        await self.db.backup(raw)
        # 기사 DB 는 gzip 으로 약 1/3 로 줄어든다
        await asyncio.to_thread(_gzip_file, raw, target)
        backups = sorted(backup_dir.glob("news-*.db.gz"))
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)
        logger.info("백업 완료 — {} ({:.1f} MB)", target, target.stat().st_size / 1e6)


def _gzip_file(src: Path, dst: Path) -> None:
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    with open(src, "rb") as fin, gzip.open(tmp, "wb", compresslevel=3) as fout:
        shutil.copyfileobj(fin, fout, length=4 * 1024 * 1024)
    tmp.replace(dst)
    src.unlink(missing_ok=True)
