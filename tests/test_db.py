from app.db import SCHEMA_VERSION, init_db, migrate

EXPECTED = {
    "users", "sources", "segments", "chunks", "chunks_fts", "chunks_vec",
    "events", "jobs", "reminder_deliveries", "login_tokens",
}


def test_schema_created(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    init_db(path).close()
    c = init_db(path)  # second open must not fail or re-run migrations
    assert migrate(c) == SCHEMA_VERSION
    c.close()


def test_check_constraints(conn, users):
    import sqlite3
    import pytest
    a, _ = users
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sources (user_id, type, content_hash, created_at) VALUES (?, 'fax', 'h', 'now')", (a,)
        )
