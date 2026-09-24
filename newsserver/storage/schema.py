"""SQLite 스키마. ``PRAGMA user_version`` 으로 마이그레이션 단계를 관리한다.

시각 컬럼은 전부 UTC ``YYYY-MM-DDTHH:MM:SSZ`` 문자열이다.
"""

MIGRATIONS: list[str] = [
    # ── v1 ──
    """
    CREATE TABLE sources (
        key              TEXT PRIMARY KEY,
        kind             TEXT NOT NULL,
        label            TEXT NOT NULL,
        market           TEXT NOT NULL,
        lang             TEXT NOT NULL,
        category         TEXT NOT NULL DEFAULT '',
        body_kind        TEXT NOT NULL,
        is_filing        INTEGER NOT NULL DEFAULT 0,
        per_symbol       INTEGER NOT NULL DEFAULT 0,
        active           INTEGER NOT NULL DEFAULT 1,   -- sources.yaml 에 존재하는지
        enabled_override INTEGER,                      -- API 토글. NULL 이면 yaml 값을 따른다
        etag             TEXT,
        last_modified    TEXT,
        last_fetch_at    TEXT,
        last_success_at  TEXT,
        last_new_at      TEXT,
        last_error       TEXT,
        fail_streak      INTEGER NOT NULL DEFAULT 0,
        idle_streak      INTEGER NOT NULL DEFAULT 0,
        next_poll_at     TEXT
    );

    CREATE TABLE articles (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        url           TEXT NOT NULL,
        url_key       TEXT NOT NULL UNIQUE,
        title         TEXT NOT NULL,
        summary       TEXT NOT NULL DEFAULT '',
        body_kind     TEXT NOT NULL,
        source_key    TEXT NOT NULL,
        market        TEXT NOT NULL,
        lang          TEXT NOT NULL,
        category      TEXT NOT NULL DEFAULT '',
        is_filing     INTEGER NOT NULL DEFAULT 0,
        published_at  TEXT,
        collected_at  TEXT NOT NULL,
        ts            TEXT NOT NULL,
        title_key     TEXT NOT NULL,
        extra_json    TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX ix_articles_ts ON articles(ts);
    CREATE INDEX ix_articles_collected ON articles(collected_at);
    CREATE INDEX ix_articles_market_ts ON articles(market, ts);
    CREATE INDEX ix_articles_source_ts ON articles(source_key, ts);

    CREATE TABLE article_feeds (
        article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        source_key TEXT NOT NULL,
        seen_at    TEXT NOT NULL,
        PRIMARY KEY (article_id, source_key)
    ) WITHOUT ROWID;
    CREATE INDEX ix_feeds_source ON article_feeds(source_key, article_id);

    CREATE TABLE article_symbols (
        article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        market     TEXT NOT NULL,
        symbol     TEXT NOT NULL,
        method     TEXT NOT NULL,
        PRIMARY KEY (article_id, market, symbol)
    ) WITHOUT ROWID;
    CREATE INDEX ix_symbols_lookup ON article_symbols(market, symbol, article_id);

    CREATE TABLE article_topics (
        article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        topic      TEXT NOT NULL,
        PRIMARY KEY (article_id, topic)
    ) WITHOUT ROWID;
    CREATE INDEX ix_topics_lookup ON article_topics(topic, article_id);

    CREATE VIRTUAL TABLE articles_fts USING fts5(
        title, summary, content='articles', content_rowid='id', tokenize='trigram'
    );
    CREATE TRIGGER articles_ai AFTER INSERT ON articles BEGIN
        INSERT INTO articles_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
    END;
    CREATE TRIGGER articles_ad AFTER DELETE ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, title, summary)
        VALUES ('delete', old.id, old.title, old.summary);
    END;
    CREATE TRIGGER articles_au AFTER UPDATE OF title, summary ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, title, summary)
        VALUES ('delete', old.id, old.title, old.summary);
        INSERT INTO articles_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
    END;

    CREATE TABLE symbols (
        market       TEXT NOT NULL,
        symbol       TEXT NOT NULL,
        name         TEXT NOT NULL,
        name_en      TEXT NOT NULL DEFAULT '',
        aliases_json TEXT NOT NULL DEFAULT '[]',
        cik          TEXT,
        corp_code    TEXT,
        rank         INTEGER,                     -- 원천 목록 순서 (SEC 는 대략 시가총액 순)
        origin       TEXT NOT NULL DEFAULT '',    -- sec | dart | manual
        updated_at   TEXT NOT NULL,
        PRIMARY KEY (market, symbol)
    ) WITHOUT ROWID;
    CREATE INDEX ix_symbols_cik ON symbols(cik);

    CREATE TABLE watchlists (
        client       TEXT NOT NULL,
        market       TEXT NOT NULL,
        symbol       TEXT NOT NULL,
        name         TEXT NOT NULL DEFAULT '',
        aliases_json TEXT NOT NULL DEFAULT '[]',
        updated_at   TEXT NOT NULL,
        PRIMARY KEY (client, market, symbol)
    ) WITHOUT ROWID;

    CREATE TABLE symbol_fetch_state (
        source_key      TEXT NOT NULL,
        market          TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        last_fetch_at   TEXT,
        last_success_at TEXT,
        last_error      TEXT,
        fail_streak     INTEGER NOT NULL DEFAULT 0,
        next_poll_at    TEXT,
        PRIMARY KEY (source_key, market, symbol)
    ) WITHOUT ROWID;

    CREATE TABLE fetch_log (
        id          INTEGER PRIMARY KEY,
        source_key  TEXT NOT NULL,
        target      TEXT NOT NULL DEFAULT '',
        started_at  TEXT NOT NULL,
        duration_ms INTEGER,
        http_status INTEGER,
        n_items     INTEGER NOT NULL DEFAULT 0,
        n_new       INTEGER NOT NULL DEFAULT 0,
        error       TEXT
    );
    CREATE INDEX ix_fetch_log_source ON fetch_log(source_key, started_at);
    CREATE INDEX ix_fetch_log_started ON fetch_log(started_at);

    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]
