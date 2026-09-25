"""SQLite 커넥션 관리.

쓰기는 커넥션 하나를 락으로 직렬화하고(단일 writer), 읽기는 읽기 전용 커넥션 풀을 쓴다.
WAL 모드라 읽기가 쓰기를 막지 않는다.
"""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite
from loguru import logger

from newsserver.storage.schema import MIGRATIONS


class Database:
    def __init__(self, path: Path, readers: int = 3):
        self.path = Path(path)
        self._n_readers = readers
        self._writer: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._readers: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._all_readers: list[aiosqlite.Connection] = []

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._open()
        except BaseException:
            # aiosqlite 커넥션마다 스레드가 있어 닫지 않으면 프로세스가 종료되지 않는다
            await self.close()
            raise

    async def _open(self) -> None:
        self._writer = await aiosqlite.connect(self.path)
        self._writer.row_factory = sqlite3.Row
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA foreign_keys=ON",
            "PRAGMA busy_timeout=10000",
            "PRAGMA auto_vacuum=INCREMENTAL",
            "PRAGMA temp_store=MEMORY",
        ):
            await self._writer.execute(pragma)
        await self._migrate()

        for _ in range(self._n_readers):
            conn = await aiosqlite.connect(f"file:{self.path}?mode=ro", uri=True)
            self._all_readers.append(conn)
            conn.row_factory = sqlite3.Row
            await conn.execute("PRAGMA busy_timeout=10000")
            await conn.execute("PRAGMA query_only=ON")
            self._readers.put_nowait(conn)

    async def _migrate(self) -> None:
        assert self._writer is not None
        cur = await self._writer.execute("PRAGMA user_version")
        version = (await cur.fetchone())[0]
        for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
            logger.info("DB 마이그레이션 v{} 적용", index)
            await self._writer.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={index};\nCOMMIT;")

    async def close(self) -> None:
        for conn in self._all_readers:
            await conn.close()
        self._all_readers.clear()
        if self._writer is not None:
            await self._writer.close()
            self._writer = None

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """쓰기 트랜잭션. 블록이 정상 종료하면 커밋, 예외면 롤백."""
        assert self._writer is not None, "Database.open() 을 먼저 호출해야 합니다"
        async with self._write_lock:
            try:
                yield self._writer
                await self._writer.commit()
            except BaseException:
                await self._writer.rollback()
                raise

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self._readers.get()
        try:
            yield conn
        finally:
            self._readers.put_nowait(conn)

    async def get_meta(self, key: str) -> str | None:
        async with self.read() as conn:
            cur = await conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self.write() as conn:
            await conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    async def backup(self, target: Path) -> None:
        """온라인 백업 (sqlite backup API).

        별도의 읽기 전용 연결에서 한 단계로 복사한다. WAL 모드에서는 읽기 트랜잭션 하나의
        스냅샷을 복사하므로 쓰기 락을 잡지 않아도 일관된 사본이 나오고, 복사하는 동안 수집이 멈추지 않는다.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        src = await aiosqlite.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            await src.execute("PRAGMA busy_timeout=10000")
            dest = await aiosqlite.connect(target)
            try:
                await src.backup(dest)
            finally:
                await dest.close()
        finally:
            await src.close()
