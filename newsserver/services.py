"""애플리케이션 서비스 묶음 (lifespan 에서 생성해 ``app.state.services`` 에 둔다)."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import httpx

from newsserver.config import Settings
from newsserver.pipeline import Ingestor
from newsserver.query import QueryService
from newsserver.scheduler import Scheduler
from newsserver.sources import SourceSpec
from newsserver.storage.db import Database
from newsserver.symbols import SymbolDirectory
from newsserver.topics import TopicRules


@dataclass
class Services:
    settings: Settings
    db: Database
    specs: dict[str, SourceSpec]
    directory: SymbolDirectory
    topics: TopicRules
    ingestor: Ingestor
    query: QueryService
    scheduler: Scheduler
    http: httpx.AsyncClient
    client_requests: Counter = field(default_factory=Counter)
