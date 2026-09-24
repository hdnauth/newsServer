"""FastAPI 애플리케이션. ``uvicorn newsserver.main:app``"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from loguru import logger

from newsserver import __version__
from newsserver.alerts import Notifier
from newsserver.api.router import root, v1
from newsserver.config import Settings, get_settings
from newsserver.pipeline import Ingestor
from newsserver.query import QueryService
from newsserver.scheduler import Scheduler
from newsserver.services import Services
from newsserver.sources import load_sources
from newsserver.storage.db import Database
from newsserver.symbols import SymbolDirectory
from newsserver.topics import TopicRules


def setup_logging(settings: Settings) -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(settings.log_dir / "server.log", level=settings.log_level, rotation="20 MB",
               retention=5, encoding="utf-8", enqueue=True)


async def build_services(settings: Settings) -> Services:
    specs = load_sources(settings.sources_file)
    topics = TopicRules.load(settings.topics_file)

    db = Database(settings.db_path)
    await db.open()

    directory = SymbolDirectory(
        wordlist=SymbolDirectory.load_wordlist(settings.wordlist_file),
        stopnames=SymbolDirectory.load_stopnames(settings.stopnames_file),
    )
    await SymbolDirectory.apply_manual_aliases(db, settings.aliases_file)
    await directory.load(db)

    http = httpx.AsyncClient(
        headers={"User-Agent": settings.http_user_agent},
        timeout=httpx.Timeout(settings.http_timeout_sec),
        follow_redirects=True,
    )
    ingestor = Ingestor(db, directory, topics, settings)
    spec_map = {s.key: s for s in specs}
    scheduler = Scheduler(db=db, settings=settings, specs=specs, ingestor=ingestor, directory=directory,
                          topics=topics, http=http, notifier=Notifier(settings, http))
    await scheduler.init_state()
    return Services(
        settings=settings, db=db, specs=spec_map, directory=directory, topics=topics, ingestor=ingestor,
        query=QueryService(db, directory, topics, spec_map), scheduler=scheduler, http=http,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = await build_services(settings)
        app.state.services = svc
        if settings.scheduler_enabled:
            svc.scheduler.start()
        logger.info("NewsServer {} 시작 — 소스 {}개, DB {}", __version__, len(svc.specs), settings.db_path)
        try:
            yield
        finally:
            await svc.scheduler.stop()
            await svc.http.aclose()
            await svc.db.close()
            logger.info("NewsServer 종료")

    app = FastAPI(title="NewsServer", version=__version__, lifespan=lifespan,
                  description="뉴스·공시를 한 번 수집해 HTTP로 제공하는 로컬 서버")

    @app.middleware("http")
    async def count_clients(request: Request, call_next):
        client = request.headers.get("x-client")
        if client and hasattr(request.app.state, "services"):
            request.app.state.services.client_requests[client[:64]] += 1
        return await call_next(request)

    app.include_router(root)
    app.include_router(v1)
    return app


def app_factory() -> FastAPI:
    """``uvicorn --factory newsserver.main:app_factory``"""
    settings = get_settings()
    setup_logging(settings)
    return create_app(settings)
