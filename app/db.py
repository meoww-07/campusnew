"""SQLite connection, schema creation and migrations.

One database file. Connections run in autocommit mode; multi-statement writes
use the `transaction()` context manager so they commit or roll back together.
Schema versions are tracked with PRAGMA user_version.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

EMBED_DIM = 384

# Each entry migrates from version i to i+1. Append only; never edit old ones.
MIGRATIONS: list[str] = [
    # v1: initial schema
    f"""
    CREATE TABLE users (
        id                  INTEGER PRIMARY KEY,
        telegram_chat_id    INTEGER NOT NULL UNIQUE,
        name                TEXT,
        timezone            TEXT NOT NULL DEFAULT 'Asia/Kolkata',
        gmail_refresh_token TEXT,
        gmail_last_sync     TEXT,
        created_at          TEXT NOT NULL
    );

    CREATE TABLE sources (
        id           INTEGER PRIMARY KEY,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        type         TEXT NOT NULL CHECK (type IN ('telegram','whatsapp','gmail','upload')),
        external_id  TEXT,
        title        TEXT,
        file_path    TEXT,
        content_hash TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        UNIQUE (user_id, content_hash)
    );
    CREATE INDEX idx_sources_user_ext ON sources(user_id, type, external_id);

    CREATE TABLE segments (
        id           INTEGER PRIMARY KEY,
        source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        text         TEXT NOT NULL,
        locator_json TEXT NOT NULL DEFAULT '{{}}',
        observed_at  TEXT NOT NULL
    );
    CREATE INDEX idx_segments_source ON segments(source_id);
    CREATE INDEX idx_segments_user ON segments(user_id, observed_at);

    CREATE TABLE chunks (
        id         INTEGER PRIMARY KEY,
        segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        text       TEXT NOT NULL,
        char_start INTEGER NOT NULL,
        char_end   INTEGER NOT NULL
    );
    CREATE INDEX idx_chunks_segment ON chunks(segment_id);

    -- rowid is set equal to chunk_id so deletes are O(1).
    CREATE VIRTUAL TABLE chunks_fts USING fts5(
        text, chunk_id UNINDEXED, user_id UNINDEXED,
        tokenize = 'porter unicode61'
    );

    -- user_id as a partition key so KNN is scoped per user before ranking.
    CREATE VIRTUAL TABLE chunks_vec USING vec0(
        chunk_id INTEGER PRIMARY KEY,
        user_id INTEGER PARTITION KEY,
        embedding FLOAT[{EMBED_DIM}] distance_metric=cosine
    );

    CREATE TABLE events (
        id          INTEGER PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        segment_id  INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
        title       TEXT NOT NULL,
        type        TEXT NOT NULL CHECK (type IN ('exam','deadline','event')),
        subject     TEXT,
        due_at      TEXT NOT NULL,          -- ISO 8601, normalized to UTC
        all_day     INTEGER NOT NULL DEFAULT 0,
        date_text   TEXT,
        confidence  REAL NOT NULL,
        status      TEXT NOT NULL DEFAULT 'upcoming'
                    CHECK (status IN ('upcoming','past','conflict','cancelled')),
        valid_until TEXT NOT NULL
    );
    CREATE INDEX idx_events_user_due ON events(user_id, status, due_at);

    CREATE TABLE jobs (
        id         INTEGER PRIMARY KEY,
        source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind       TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','processing','done','failed')),
        attempts   INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        locked_at  TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_jobs_status ON jobs(status, created_at);

    CREATE TABLE reminder_deliveries (
        id       INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        kind     TEXT NOT NULL,
        sent_at  TEXT NOT NULL,
        UNIQUE (event_id, kind)
    );

    CREATE TABLE login_tokens (
        token      TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires_at TEXT NOT NULL
    );
    """,
    # v2: file jobs carry the context the worker needs to build segments
    # (observed_at, base locator, caption) since files have no segments yet.
    """
    ALTER TABLE jobs ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}';
    """,
    # v3: LLM extraction results per segment (also used as search filters).
    """
    ALTER TABLE segments ADD COLUMN summary TEXT;
    ALTER TABLE segments ADD COLUMN category TEXT;
    ALTER TABLE segments ADD COLUMN subject TEXT;
    ALTER TABLE segments ADD COLUMN importance TEXT;
    ALTER TABLE segments ADD COLUMN extracted_at TEXT;
    CREATE INDEX idx_segments_importance ON segments(user_id, importance);
    """,
    # v4: Smart Spaces. jobs is rebuilt (SQLite can't relax NOT NULL in place) so
    # that space backfill jobs need no source. All existing job rows are kept.
    """
    CREATE TABLE spaces (
        id               INTEGER PRIMARY KEY,
        user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name             TEXT NOT NULL,
        kind             TEXT NOT NULL CHECK (kind IN ('auto','custom')),
        auto_key         TEXT,             -- 'subject:dbms' / 'category:exam'; stable across renames
        icon             TEXT,
        color            TEXT,
        rules_json       TEXT NOT NULL DEFAULT '{}',
        pinned           INTEGER NOT NULL DEFAULT 0,
        merged_into      INTEGER REFERENCES spaces(id) ON DELETE SET NULL,
        deleted_at       TEXT,             -- auto spaces are soft-deleted so they aren't re-created
        last_activity_at TEXT,
        created_at       TEXT NOT NULL,
        UNIQUE (user_id, name)
    );
    CREATE UNIQUE INDEX idx_spaces_auto_key ON spaces(user_id, auto_key) WHERE auto_key IS NOT NULL;

    CREATE TABLE space_items (
        space_id   INTEGER NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
        segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
        added_by   TEXT NOT NULL CHECK (added_by IN ('auto','rule','user')),
        score      REAL NOT NULL DEFAULT 1.0,
        added_at   TEXT NOT NULL,
        PRIMARY KEY (space_id, segment_id)
    );
    CREATE INDEX idx_space_items_segment ON space_items(segment_id);

    CREATE TABLE space_exclusions (
        space_id   INTEGER NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
        segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        PRIMARY KEY (space_id, segment_id)
    );

    -- Normalized subjects per segment (segment subject + its events' subjects).
    -- Lets an auto space appear once a subject reaches 2 items.
    CREATE TABLE segment_subjects (
        segment_id   INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        subject_key  TEXT NOT NULL,
        subject_name TEXT NOT NULL,
        PRIMARY KEY (segment_id, subject_key)
    );
    CREATE INDEX idx_segment_subjects_key ON segment_subjects(user_id, subject_key);

    CREATE TABLE jobs_new (
        id           INTEGER PRIMARY KEY,
        source_id    INTEGER REFERENCES sources(id) ON DELETE CASCADE,
        space_id     INTEGER REFERENCES spaces(id) ON DELETE CASCADE,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind         TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','processing','done','failed')),
        attempts     INTEGER NOT NULL DEFAULT 0,
        last_error   TEXT,
        locked_at    TEXT,
        created_at   TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}'
    );
    INSERT INTO jobs_new (id, source_id, user_id, kind, status, attempts, last_error, locked_at, created_at, payload_json)
        SELECT id, source_id, user_id, kind, status, attempts, last_error, locked_at, created_at, payload_json FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_new RENAME TO jobs;
    CREATE INDEX idx_jobs_status ON jobs(status, created_at);
    CREATE INDEX idx_jobs_source ON jobs(source_id);

    -- One-time backfill: category spaces + auto spaces for already-processed data.
    INSERT INTO jobs (user_id, kind, status, created_at)
        SELECT id, 'spaces_backfill', 'pending', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') FROM users;
    """,
    # v5: superseding. A newer "postponed to ..." message retires the old date
    # (status 'cancelled', superseded_by -> the new event) instead of a conflict.
    """
    ALTER TABLE events ADD COLUMN superseded_by INTEGER REFERENCES events(id) ON DELETE SET NULL;
    CREATE INDEX idx_events_superseded ON events(superseded_by);
    INSERT INTO jobs (user_id, kind, status, created_at)
        SELECT id, 'resolve_conflicts', 'pending', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') FROM users;
    """,
    # v6: Calendar + Today. events is rebuilt (new 'done' status, nullable segment for
    # manual events, edit tracking, version for reminder resets); reminder_deliveries is
    # rebuilt so uniqueness is per event *version*. All existing rows are kept.
    """
    CREATE TABLE events_new (
        id            INTEGER PRIMARY KEY,
        user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        segment_id    INTEGER REFERENCES segments(id) ON DELETE CASCADE,   -- NULL for manual events
        title         TEXT NOT NULL,
        type          TEXT NOT NULL CHECK (type IN ('exam','deadline','event')),
        subject       TEXT,
        due_at        TEXT NOT NULL,          -- ISO 8601, UTC
        end_at        TEXT,
        all_day       INTEGER NOT NULL DEFAULT 0,
        location      TEXT,
        date_text     TEXT,
        confidence    REAL NOT NULL DEFAULT 1.0,
        status        TEXT NOT NULL DEFAULT 'upcoming'
                      CHECK (status IN ('upcoming','past','conflict','cancelled','done')),
        valid_until   TEXT NOT NULL,
        superseded_by INTEGER REFERENCES events(id) ON DELETE SET NULL,
        user_edited   INTEGER NOT NULL DEFAULT 0,
        version       INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT,
        updated_at    TEXT
    );
    INSERT INTO events_new (id, user_id, segment_id, title, type, subject, due_at, all_day, date_text,
                            confidence, status, valid_until, superseded_by)
        SELECT id, user_id, segment_id, title, type, subject, due_at, all_day, date_text,
               confidence, status, valid_until, superseded_by FROM events;
    DROP TABLE events;
    ALTER TABLE events_new RENAME TO events;
    CREATE INDEX idx_events_user_due ON events(user_id, due_at);
    CREATE INDEX idx_events_user_status_due ON events(user_id, status, due_at);
    CREATE INDEX idx_events_segment ON events(segment_id);
    CREATE INDEX idx_events_superseded ON events(superseded_by);

    CREATE TABLE reminder_deliveries_new (
        id            INTEGER PRIMARY KEY,
        event_id      INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        event_version INTEGER NOT NULL DEFAULT 1,
        kind          TEXT NOT NULL,
        sent_at       TEXT NOT NULL,
        UNIQUE (event_id, event_version, kind)
    );
    INSERT INTO reminder_deliveries_new (id, event_id, event_version, kind, sent_at)
        SELECT id, event_id, 1, kind, sent_at FROM reminder_deliveries;
    DROP TABLE reminder_deliveries;
    ALTER TABLE reminder_deliveries_new RENAME TO reminder_deliveries;

    ALTER TABLE users ADD COLUMN ical_token TEXT;
    ALTER TABLE users ADD COLUMN last_seen_today_at TEXT;
    CREATE UNIQUE INDEX idx_users_ical_token ON users(ical_token) WHERE ical_token IS NOT NULL;
    ALTER TABLE segments ADD COLUMN opened_at TEXT;

    -- "Snooze to tomorrow" on the Today screen.
    CREATE TABLE snoozes (
        user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        item_kind TEXT NOT NULL CHECK (item_kind IN ('event','segment')),
        item_id   INTEGER NOT NULL,
        until     TEXT NOT NULL,
        PRIMARY KEY (user_id, item_kind, item_id)
    );
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with sqlite-vec loaded, FKs on, WAL, Row factory."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Idempotent. Returns the resulting version."""
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return current
    # Table rebuilds (create new, copy, drop, rename) need FK enforcement off;
    # it can't be toggled inside a transaction, so it brackets the whole run.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for version in range(current, SCHEMA_VERSION):
            # executescript issues its own COMMIT, so wrap each migration manually.
            conn.executescript(
                f"BEGIN;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;"
            )
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(f"migration v{version + 1} broke foreign keys: {[tuple(r) for r in broken[:5]]}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
