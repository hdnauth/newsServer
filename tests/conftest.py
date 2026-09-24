from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from newsserver.config import ROOT, Settings
from newsserver.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        db_path=tmp_path / "news.db",
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
        sources_file=ROOT / "config" / "sources.yaml",
        topics_file=ROOT / "config" / "topics.yaml",
        aliases_file=FIXTURES / "aliases.yaml",
        stopnames_file=tmp_path / "none.txt",
        wordlist_file=FIXTURES / "words.txt",
        scheduler_enabled=False,
        dart_api_key="",
        edgar_user_agent="",
    )


@pytest.fixture
async def app_ctx(settings):
    """lifespan 을 돌린 앱과 httpx 클라이언트. (client, services) 를 넘긴다."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app.state.services
