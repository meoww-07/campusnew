"""ALL database access lives here.

Rule: every function that reads or writes user data takes `user_id` and
filters by it. The only exceptions are identity/system entry points, marked
[system]: user lookup by Telegram chat id, login-token consumption, the
worker's job claiming, and listing user ids for schedulers.

Timestamps are stored as ISO 8601 strings in UTC ("...+00:00") so that
string comparison equals time comparison.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlite_vec

from app.db import transaction
from app.models import (
    AddedBy, Chunk, ChunkHit, ChunkIn, Event, EventIn, EventStatus, IngestResult, Job,
    Segment, SegmentContext, SegmentIn, Source, SourceType, Space, SpaceKind, SpaceRules, User,
)

MAX_JOB_ATTEMPTS = 3
JOB_LOCK_TIMEOUT = timedelta(minutes=10)


# ---------- helpers ----------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Normalize an aware datetime to a UTC ISO string."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed; attach a timezone")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = " ".join(data.split()).encode("utf-8")  # whitespace-insensitive
    return hashlib.sha256(data).hexdigest()


def _job(row: sqlite3.Row) -> Job:
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json") or "{}")
    return Job(**d)


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _segment(row: sqlite3.Row) -> Segment:
    d = dict(row)
    d["locator"] = json.loads(d.pop("locator_json") or "{}")
    return Segment(**d)


# ---------- users ----------

def upsert_user(conn: sqlite3.Connection, telegram_chat_id: int, name: str | None) -> User:
    """[system] Register a Telegram chat (idempotent); updates the name."""
    conn.execute(
        """INSERT INTO users (telegram_chat_id, name, created_at) VALUES (?, ?, ?)
           ON CONFLICT(telegram_chat_id) DO UPDATE SET name = COALESCE(excluded.name, users.name)""",
        (telegram_chat_id, name, iso(utcnow())),
    )
    user = get_user_by_chat_id(conn, telegram_chat_id)
    assert user is not None
    return user


def get_user_by_chat_id(conn: sqlite3.Connection, telegram_chat_id: int) -> User | None:
    """[system] Identity lookup for incoming Telegram updates."""
    row = conn.execute("SELECT * FROM users WHERE telegram_chat_id = ?", (telegram_chat_id,)).fetchone()
    return User(**dict(row)) if row else None


def get_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User(**dict(row)) if row else None


def list_user_ids(conn: sqlite3.Connection, gmail_connected: bool = False) -> list[int]:
    """[system] For schedulers that iterate users and then call scoped functions."""
    sql = "SELECT id FROM users"
    if gmail_connected:
        sql += " WHERE gmail_refresh_token IS NOT NULL"
    return [r[0] for r in conn.execute(sql)]


def set_gmail_token(conn: sqlite3.Connection, user_id: int, refresh_token: str | None) -> None:
    conn.execute("UPDATE users SET gmail_refresh_token = ? WHERE id = ?", (refresh_token, user_id))


def set_gmail_last_sync(conn: sqlite3.Connection, user_id: int, when: datetime) -> None:
    conn.execute("UPDATE users SET gmail_last_sync = ? WHERE id = ?", (iso(when), user_id))


# ---------- sources + ingestion ----------

def ingest_source(
    conn: sqlite3.Connection,
    user_id: int,
    type: SourceType,
    content_hash: str,
    segments: Sequence[SegmentIn] = (),
    *,
    external_id: str | None = None,
    title: str | None = None,
    file_path: str | None = None,
    job_kind: str = "process",
    job_payload: dict[str, object] | None = None,
) -> IngestResult:
    """Insert source + its segments + a pending job in ONE transaction.

    Duplicate content (same user + hash) stores nothing and returns
    created=False. Callers reply "Saved ✓" only after this returns.
    """
    now = iso(utcnow())
    with transaction(conn):
        row = conn.execute(
            """INSERT INTO sources (user_id, type, external_id, title, file_path, content_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, content_hash) DO NOTHING
               RETURNING id""",
            (user_id, type, external_id, title, file_path, content_hash, now),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                "SELECT id FROM sources WHERE user_id = ? AND content_hash = ?",
                (user_id, content_hash),
            ).fetchone()
            return IngestResult(source_id=existing[0], created=False)
        source_id: int = row[0]
        _insert_segments(conn, user_id, source_id, segments)
        job_id = conn.execute(
            """INSERT INTO jobs (source_id, user_id, kind, status, created_at, payload_json)
               VALUES (?, ?, ?, 'pending', ?, ?) RETURNING id""",
            (source_id, user_id, job_kind, now, json.dumps(job_payload or {}, ensure_ascii=False)),
        ).fetchone()[0]
    return IngestResult(source_id=source_id, created=True, job_id=job_id)


def get_source(conn: sqlite3.Connection, user_id: int, source_id: int) -> Source | None:
    row = conn.execute(
        "SELECT * FROM sources WHERE id = ? AND user_id = ?", (source_id, user_id)
    ).fetchone()
    return Source(**dict(row)) if row else None


def list_sources(conn: sqlite3.Connection, user_id: int, limit: int = 200) -> list[Source]:
    rows = conn.execute(
        "SELECT * FROM sources WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, limit),
    )
    return [Source(**dict(r)) for r in rows]


def list_sources_overview(conn: sqlite3.Connection, user_id: int, limit: int = 200) -> list[dict]:
    """Sources with segment/event counts and processing status, newest first (for the web page)."""
    rows = conn.execute(
        """SELECT src.id, src.type, src.title, src.created_at,
                  (SELECT count(*) FROM segments s WHERE s.source_id = src.id) AS segments,
                  (SELECT count(*) FROM events e JOIN segments s ON s.id = e.segment_id
                    WHERE s.source_id = src.id) AS events,
                  (SELECT j.status FROM jobs j WHERE j.source_id = src.id ORDER BY j.id DESC LIMIT 1) AS status,
                  (SELECT j.last_error FROM jobs j WHERE j.source_id = src.id ORDER BY j.id DESC LIMIT 1) AS last_error
           FROM sources src WHERE src.user_id = ?
           ORDER BY src.created_at DESC, src.id DESC LIMIT ?""",
        (user_id, limit),
    )
    return [dict(r) for r in rows]


def external_id_exists(conn: sqlite3.Connection, user_id: int, type: SourceType, external_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sources WHERE user_id = ? AND type = ? AND external_id = ?",
        (user_id, type, external_id),
    ).fetchone() is not None


def delete_source(conn: sqlite3.Connection, user_id: int, source_id: int) -> bool:
    """Delete a source and everything derived from it, including the file.

    Segments, chunks, events, jobs and reminder deliveries go via FK cascade;
    FTS and vector rows are virtual tables, so they are removed explicitly.
    """
    with transaction(conn):
        src = conn.execute(
            "SELECT file_path FROM sources WHERE id = ? AND user_id = ?", (source_id, user_id)
        ).fetchone()
        if src is None:
            return False
        chunk_ids = [r[0] for r in conn.execute(
            """SELECT c.id FROM chunks c JOIN segments s ON s.id = c.segment_id
               WHERE s.source_id = ? AND c.user_id = ?""",
            (source_id, user_id),
        )]
        for i in range(0, len(chunk_ids), 500):
            batch = chunk_ids[i:i + 500]
            ph = _placeholders(len(batch))
            conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({ph})", batch)
            conn.execute(f"DELETE FROM chunks_vec WHERE chunk_id IN ({ph})", batch)
        conn.execute("DELETE FROM sources WHERE id = ? AND user_id = ?", (source_id, user_id))
    # Remove the file (or attachment folder) only after the DB commit succeeded.
    if src["file_path"]:
        path = Path(src["file_path"])
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    return True


# ---------- segments ----------

def _insert_segments(
    conn: sqlite3.Connection, user_id: int, source_id: int, segments: Sequence[SegmentIn]
) -> list[int]:
    return [
        conn.execute(
            """INSERT INTO segments (source_id, user_id, text, locator_json, observed_at)
               VALUES (?, ?, ?, ?, ?) RETURNING id""",
            (source_id, user_id, s.text, json.dumps(s.locator, ensure_ascii=False), iso(s.observed_at)),
        ).fetchone()[0]
        for s in segments
    ]


def add_segments(
    conn: sqlite3.Connection, user_id: int, source_id: int, segments: Sequence[SegmentIn]
) -> list[int]:
    """Used by the worker (e.g. one segment per PDF page). Verifies ownership."""
    with transaction(conn):
        if conn.execute(
            "SELECT 1 FROM sources WHERE id = ? AND user_id = ?", (source_id, user_id)
        ).fetchone() is None:
            raise PermissionError(f"source {source_id} not owned by user {user_id}")
        return _insert_segments(conn, user_id, source_id, segments)


def list_segments(conn: sqlite3.Connection, user_id: int, source_id: int) -> list[Segment]:
    rows = conn.execute(
        "SELECT * FROM segments WHERE source_id = ? AND user_id = ? ORDER BY id",
        (source_id, user_id),
    )
    return [_segment(r) for r in rows]


def list_unindexed_segments(conn: sqlite3.Connection, user_id: int, source_id: int) -> list[Segment]:
    """Segments of a source that have no chunks yet (makes worker retries idempotent)."""
    rows = conn.execute(
        """SELECT s.* FROM segments s
           WHERE s.source_id = ? AND s.user_id = ?
             AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.segment_id = s.id)
           ORDER BY s.id""",
        (source_id, user_id),
    )
    return [_segment(r) for r in rows]


def next_segment(conn: sqlite3.Connection, user_id: int, segment_id: int) -> Segment | None:
    """The following segment of the same source (e.g. the reply after a chat question)."""
    row = conn.execute(
        """SELECT n.* FROM segments s JOIN segments n ON n.source_id = s.source_id AND n.id > s.id
           WHERE s.id = ? AND s.user_id = ? AND n.user_id = ? ORDER BY n.id LIMIT 1""",
        (segment_id, user_id, user_id),
    ).fetchone()
    return _segment(row) if row else None


def get_segment(conn: sqlite3.Connection, user_id: int, segment_id: int) -> Segment | None:
    row = conn.execute(
        "SELECT * FROM segments WHERE id = ? AND user_id = ?", (segment_id, user_id)
    ).fetchone()
    return _segment(row) if row else None


# ---------- chunks + index ----------

def add_chunks(
    conn: sqlite3.Connection,
    user_id: int,
    segment_id: int,
    chunks: Sequence[ChunkIn],
    embeddings: Sequence[Sequence[float]] | None = None,
) -> list[int]:
    """Insert chunks and index them (FTS5 always, vectors if given) atomically."""
    if embeddings is not None and len(embeddings) != len(chunks):
        raise ValueError("embeddings and chunks length mismatch")
    with transaction(conn):
        if conn.execute(
            "SELECT 1 FROM segments WHERE id = ? AND user_id = ?", (segment_id, user_id)
        ).fetchone() is None:
            raise PermissionError(f"segment {segment_id} not owned by user {user_id}")
        ids: list[int] = []
        for i, c in enumerate(chunks):
            cid = conn.execute(
                """INSERT INTO chunks (segment_id, user_id, text, char_start, char_end)
                   VALUES (?, ?, ?, ?, ?) RETURNING id""",
                (segment_id, user_id, c.text, c.char_start, c.char_end),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO chunks_fts (rowid, text, chunk_id, user_id) VALUES (?, ?, ?, ?)",
                (cid, c.text, cid, user_id),
            )
            if embeddings is not None:
                conn.execute(
                    "INSERT INTO chunks_vec (chunk_id, user_id, embedding) VALUES (?, ?, ?)",
                    (cid, user_id, sqlite_vec.serialize_float32(list(embeddings[i]))),
                )
            ids.append(cid)
    return ids


def list_chunks(conn: sqlite3.Connection, user_id: int, segment_id: int) -> list[Chunk]:
    rows = conn.execute(
        "SELECT * FROM chunks WHERE segment_id = ? AND user_id = ? ORDER BY char_start",
        (segment_id, user_id),
    )
    return [Chunk(**dict(r)) for r in rows]


def fts_search(conn: sqlite3.Connection, user_id: int, match: str, limit: int = 50) -> list[tuple[int, float]]:
    """(chunk_id, bm25) best first. `match` must already be FTS5-sanitized."""
    rows = conn.execute(
        """SELECT rowid, bm25(chunks_fts) AS score FROM chunks_fts
           WHERE chunks_fts MATCH ? AND user_id = ?
           ORDER BY score LIMIT ?""",
        (match, user_id, limit),
    )
    return [(r[0], r[1]) for r in rows]


def vec_search(
    conn: sqlite3.Connection, user_id: int, embedding: Sequence[float], limit: int = 50
) -> list[tuple[int, float]]:
    """(chunk_id, cosine distance) nearest first, within this user's partition."""
    rows = conn.execute(
        """SELECT chunk_id, distance FROM chunks_vec
           WHERE embedding MATCH ? AND k = ? AND user_id = ?
           ORDER BY distance""",
        (sqlite_vec.serialize_float32(list(embedding)), limit, user_id),
    )
    return [(r[0], r[1]) for r in rows]


def get_chunk_hits(conn: sqlite3.Connection, user_id: int, chunk_ids: Sequence[int]) -> dict[int, ChunkHit]:
    """Hydrate chunk ids with segment + source info (only the user's own)."""
    if not chunk_ids:
        return {}
    rows = conn.execute(
        f"""SELECT c.id AS chunk_id, c.text, c.char_start, s.text AS segment_text, s.id AS segment_id,
                   s.locator_json, s.observed_at,
                   src.id AS source_id, src.type AS source_type, src.title AS source_title,
                   s.category, src.created_at
            FROM chunks c
            JOIN segments s ON s.id = c.segment_id
            JOIN sources src ON src.id = s.source_id
            WHERE c.user_id = ? AND c.id IN ({_placeholders(len(chunk_ids))})""",
        (user_id, *chunk_ids),
    )
    out: dict[int, ChunkHit] = {}
    for r in rows:
        d = dict(r)
        d["locator"] = json.loads(d.pop("locator_json") or "{}")
        segment_text = d.pop("segment_text")
        if d.pop("char_start") == SUMMARY_CHUNK:  # matched the item's summary: show the item itself
            d["text"] = f"{d['text']}\n{segment_text[:1500]}"
        out[d["chunk_id"]] = ChunkHit(**d)
    return out


SUMMARY_CHUNK = -1  # char_start marker for the indexed LLM summary of a segment


def add_summary_chunk(conn: sqlite3.Connection, user_id: int, segment_id: int, summary: str,
                      embedding: Sequence[float]) -> int | None:
    """Index a segment's LLM summary as an extra, searchable chunk (once per segment)."""
    if conn.execute("SELECT 1 FROM chunks WHERE segment_id = ? AND user_id = ? AND char_start = ?",
                    (segment_id, user_id, SUMMARY_CHUNK)).fetchone():
        return None
    return add_chunks(conn, user_id, segment_id,
                      [ChunkIn(text=summary, char_start=SUMMARY_CHUNK, char_end=SUMMARY_CHUNK)], [embedding])[0]


# ---------- events ----------

_CODE = re.compile(r"\(.*?\)|\b[a-z]{2,4}\s?-?\d{3,4}\b")
_FILLER = {"of", "and", "the", "&", "for", "to", "in", "course", "theory"}
_KINDS = [("quiz", r"quiz"), ("viva", r"viva"), ("lab", r"\blab\b|practical"),
          ("midsem", r"mid[\s-]?sem|mid[\s-]?term"), ("endsem", r"end[\s-]?sem|final exam|end[\s-]?term"),
          ("assignment", r"assignment|homework"), ("review", r"review|presentation"), ("project", r"project"),
          ("test", r"\btest\b"), ("registration", r"registration|register")]


def assessment_kind(title: str) -> str | None:
    """'quiz#2', 'midsem', ... so Quiz 1 vs Quiz 2 or viva vs midsem never 'conflict'."""
    t = title.lower()
    for kind, pattern in _KINDS:
        m = re.search(rf"(?:{pattern})\w*[\s#:-]*(\d+)?", t)
        if m:
            return f"{kind}#{m.group(1)}" if m.group(1) else kind
    return None


def subject_words(subject: str) -> list[str]:
    s = _CODE.sub(" ", subject.lower())
    return [w for w in re.findall(r"[a-z]+", s) if w not in _FILLER]


def _abbrev_of(abbr: str, words: list[str]) -> bool:
    """True if `abbr` is built from the words in order, each word contributing a
    subsequence that starts with its first letter (DBMS ~ DataBase Management Systems)."""
    if not words:
        return not abbr
    if not abbr or abbr[0] != words[0][0]:
        return False
    word = words[0]
    for take in range(1, len(abbr) + 1):
        it = iter(word)
        if all(ch in it for ch in abbr[:take]) and _abbrev_of(abbr[take:], words[1:]):
            return True
    return False


def same_subject(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    if any(re.search(r",|/|&|\band\b", s) for s in (a, b)):
        return False  # "DBMS, OS" names several courses; never compare it
    wa, wb = subject_words(a), subject_words(b)
    if not wa or not wb:
        return a.strip().lower() == b.strip().lower()
    if wa == wb:
        return True
    if len(wa) == 1 and len(wb) > 1:
        return _abbrev_of(wa[0], wb)
    if len(wb) == 1 and len(wa) > 1:
        return _abbrev_of(wb[0], wa)
    return False


_KIND_WORDS = re.compile(r"\b(mid[\s-]?sem\w*|end[\s-]?sem\w*|exam\w*|quiz|viva|test|assignment|deadline|"
                         r"submission|review|lab|record|class|extra|first|second|final|rescheduled|postponed|\d+)\b")


def effective_subject(subject: str | None, title: str) -> str | None:
    """The subject, or, when it is only a course code ('CS301'), the course name from the title
    ('CS301 Database Management Systems midsem' -> 'database management systems')."""
    if subject and subject_words(subject):
        return subject
    words = _KIND_WORDS.sub(" ", _CODE.sub(" ", title.lower()))
    words = " ".join(w for w in re.findall(r"[a-z]+", words) if w not in _FILLER)
    return words or subject


def same_moment(a_iso: str, a_all_day: bool, b_iso: str, b_all_day: bool) -> bool:
    """Within 10 minutes (LLM rounding noise), or the same day when either side has no
    time of day (an all-day "22 Oct" and "22 Oct, 10 am" are the same exam). A real time
    change such as 3 pm -> 4 pm is NOT the same moment."""
    diff = abs(datetime.fromisoformat(a_iso) - datetime.fromisoformat(b_iso))
    return diff <= timedelta(minutes=10) or ((a_all_day or b_all_day) and diff < timedelta(hours=16))


def _add_event_tx(conn: sqlite3.Connection, user_id: int, ev: EventIn, now: datetime) -> int:
    """Event insert; caller holds the transaction. See add_event."""
    if conn.execute(
        "SELECT 1 FROM segments WHERE id = ? AND user_id = ?", (ev.segment_id, user_id)
    ).fetchone() is None:
        raise PermissionError(f"segment {ev.segment_id} not owned by user {user_id}")
    due = iso(ev.due_at)
    valid_until = iso(ev.due_at + timedelta(days=1))
    subject = effective_subject(ev.subject, ev.title)
    active = [dict(r) for r in conn.execute(
        """SELECT id, title, subject, due_at, all_day, status, user_edited FROM events
           WHERE user_id = ? AND type = ? AND status IN ('upcoming','conflict','past','done')""",
        (user_id, ev.type),
    )]
    for e in active:  # same thing announced twice -> keep one row
        if same_moment(e["due_at"], bool(e["all_day"]), due, ev.all_day) and (
                same_subject(effective_subject(e["subject"], e["title"]), subject)
                or e["title"].strip().lower() == ev.title.strip().lower()):
            if e["all_day"] and not ev.all_day and not e["user_edited"]:  # later mention adds a time; never override user edits
                conn.execute("UPDATE events SET due_at = ?, all_day = 0, valid_until = ? WHERE id = ?",
                             (due, valid_until, e["id"]))
            return e["id"]
    new = {"title": ev.title, "subject": ev.subject, "due_at": due, "all_day": ev.all_day}
    if valid_until < iso(now):
        status: EventStatus = "past"
        clashing: list[int] = []
    else:
        clashing = [e["id"] for e in active if e["status"] not in ("past", "done") and _clashes(e, new)]
        status = "conflict" if clashing else "upcoming"
    event_id = conn.execute(
        """INSERT INTO events (user_id, segment_id, title, type, subject, due_at, all_day,
                               date_text, confidence, status, valid_until)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (user_id, ev.segment_id, ev.title, ev.type, ev.subject, due, int(ev.all_day),
         ev.date_text, ev.confidence, status, valid_until),
    ).fetchone()[0]
    for other in clashing:
        _resolve_pair(conn, user_id, event_id, other)
    return event_id


# A newer message announcing a change replaces the older date ("superseding").
UPDATE_WORDS = re.compile(
    r"\b(postponed|preponed|re-?scheduled|shifted|moved|changed|revised|updated?|new date|deferred|"
    r"extended|brought forward|instead of|now (?:on|at|scheduled)|will now)\b", re.IGNORECASE)


def _clashes(a: dict, b: dict) -> bool:
    """Same subject and assessment, genuinely different time (see same_moment)."""
    return (not same_moment(a["due_at"], bool(a["all_day"]), b["due_at"], bool(b["all_day"]))
            and same_subject(effective_subject(a["subject"], a["title"]), effective_subject(b["subject"], b["title"]))
            and assessment_kind(a["title"]) == assessment_kind(b["title"]))


def _source_of(conn: sqlite3.Connection, user_id: int, event_id: int) -> sqlite3.Row:
    return conn.execute(
        """SELECT COALESCE(s.text, e.title) AS text, COALESCE(s.observed_at, e.created_at, '') AS observed_at,
                  e.date_text, e.user_edited, s.locator_json, src.type AS source_type
           FROM events e LEFT JOIN segments s ON s.id = e.segment_id LEFT JOIN sources src ON src.id = s.source_id
           WHERE e.id = ? AND e.user_id = ?""", (event_id, user_id)).fetchone()


def _author(row: sqlite3.Row) -> str | None:
    """Who wrote the message behind an event: chat sender, email sender, forwarded-from,
    or the student themself for a message typed straight into the bot."""
    if row is None or row["locator_json"] is None:
        return None
    loc = json.loads(row["locator_json"] or "{}")
    for key in ("forward_sender", "sender", "from"):
        if loc.get(key):
            return f"{key}:{str(loc[key]).strip().lower()}"
    if row["source_type"] == "telegram" and loc.get("chat_id") is not None:
        return f"self:{loc['chat_id']}"
    return None


def _supersede(conn: sqlite3.Connection, user_id: int, old_id: int, new_id: int) -> None:
    conn.execute("UPDATE events SET status = 'cancelled', superseded_by = ? WHERE id = ? AND user_id = ?",
                 (new_id, old_id, user_id))


def _resolve_pair(conn: sqlite3.Connection, user_id: int, a_id: int, b_id: int) -> str:
    """Two clashing events: the one from a newer message that announces a change wins
    (the other is retired, not deleted). Otherwise both are flagged 'conflict'."""
    a, b = _source_of(conn, user_id, a_id), _source_of(conn, user_id, b_id)
    same_author = _author(a) is not None and _author(a) == _author(b)
    # A newer message wins if it says it is a change, or if the same person wrote both
    # (people correct themselves: "class on Sat 3pm" ... "class is on Sat 4pm").
    a_update = same_author or bool(UPDATE_WORDS.search(f"{a['text']} {a['date_text'] or ''}"))
    b_update = same_author or bool(UPDATE_WORDS.search(f"{b['text']} {b['date_text'] or ''}"))
    if a_update and a["observed_at"] > b["observed_at"] and not b["user_edited"]:
        _supersede(conn, user_id, b_id, a_id)
        winner = a_id
    elif b_update and b["observed_at"] > a["observed_at"] and not a["user_edited"]:
        _supersede(conn, user_id, a_id, b_id)
        winner = b_id
    else:
        conn.execute("UPDATE events SET status = 'conflict' WHERE user_id = ? AND id IN (?, ?) AND status <> 'cancelled'",
                     (user_id, a_id, b_id))
        return "conflict"
    _settle(conn, user_id, winner)
    return "superseded"


def _settle(conn: sqlite3.Connection, user_id: int, event_id: int) -> None:
    """A 'conflict' event whose every clashing partner is gone goes back to 'upcoming'."""
    e = conn.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user_id)).fetchone()
    if e is None or e["status"] != "conflict":
        return
    others = conn.execute(
        "SELECT * FROM events WHERE user_id = ? AND type = ? AND status = 'conflict' AND id <> ?",
        (user_id, e["type"], event_id)).fetchall()
    if not any(_clashes(dict(o), dict(e)) for o in others):
        conn.execute("UPDATE events SET status = 'upcoming' WHERE id = ?", (event_id,))


def resolve_conflicts(conn: sqlite3.Connection, user_id: int) -> int:
    """Re-check existing conflicts (one-time job after migration v5). Returns events superseded."""
    rows = [dict(r) for r in conn.execute(
        """SELECT e.* FROM events e JOIN segments s ON s.id = e.segment_id
           WHERE e.user_id = ? AND e.status = 'conflict' ORDER BY s.observed_at, e.id""", (user_id,))]
    n = 0
    with transaction(conn):
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                live = {r["id"]: r["status"] for r in conn.execute(
                    "SELECT id, status FROM events WHERE id IN (?, ?)", (a["id"], b["id"]))}
                if a["type"] == b["type"] and "cancelled" not in live.values() and _clashes(a, b):
                    n += _resolve_pair(conn, user_id, b["id"], a["id"]) == "superseded"
        for r in rows:
            _settle(conn, user_id, r["id"])
    return n


def undo_supersede(conn: sqlite3.Connection, user_id: int, event_id: int) -> bool:
    """'Not an update': restore a superseded event; both dates go back to 'conflict'."""
    e = conn.execute("SELECT superseded_by FROM events WHERE id = ? AND user_id = ? AND superseded_by IS NOT NULL",
                     (event_id, user_id)).fetchone()
    if e is None:
        return False
    conn.execute("UPDATE events SET status = 'conflict', superseded_by = NULL WHERE id = ? AND user_id = ?",
                 (event_id, user_id))
    conn.execute("UPDATE events SET status = 'conflict' WHERE id = ? AND user_id = ? AND status = 'upcoming'",
                 (e["superseded_by"], user_id))
    return True


def superseded_by_map(conn: sqlite3.Connection, user_id: int, event_ids: Sequence[int]) -> dict[int, list[Event]]:
    """event_id -> the earlier events it replaced (for 'moved from 21 Oct' notes)."""
    if not event_ids:
        return {}
    out: dict[int, list[Event]] = {}
    for r in conn.execute(
        f"SELECT * FROM events WHERE user_id = ? AND superseded_by IN ({_placeholders(len(event_ids))}) ORDER BY due_at",
        (user_id, *event_ids),
    ):
        out.setdefault(r["superseded_by"], []).append(Event(**dict(r)))
    return out


def add_event(conn: sqlite3.Connection, user_id: int, ev: EventIn, now: datetime | None = None) -> int:
    """Insert an event; valid_until = due_at + 1 day.

    - An identical event (same type, subject/title and due_at) is not duplicated.
    - If an upcoming/conflict event with the same subject, type and assessment exists
      at a different time: when the newer message announces a change ("postponed to…"),
      the older event is superseded (status 'cancelled', superseded_by = the new one);
      otherwise both are marked 'conflict'. Nothing is deleted.
    - Events already over at insert time are stored as 'past'.
    """
    with transaction(conn):
        return _add_event_tx(conn, user_id, ev, now or utcnow())


def apply_extraction(
    conn: sqlite3.Connection, user_id: int, segment_id: int, *,
    summary: str | None, category: str | None, subject: str | None, importance: str | None,
    events: Sequence[EventIn] = (), now: datetime | None = None,
) -> list[int]:
    """Store LLM results for a segment and its events atomically; marks it extracted."""
    now = now or utcnow()
    with transaction(conn):
        cur = conn.execute(
            """UPDATE segments SET summary = ?, category = ?, subject = ?, importance = ?, extracted_at = ?
               WHERE id = ? AND user_id = ?""",
            (summary, category, subject, importance, iso(now), segment_id, user_id),
        )
        if cur.rowcount != 1:
            raise PermissionError(f"segment {segment_id} not owned by user {user_id}")
        return [_add_event_tx(conn, user_id, ev, now) for ev in events]


def list_unextracted_segments(conn: sqlite3.Connection, user_id: int, source_id: int) -> list[Segment]:
    rows = conn.execute(
        "SELECT * FROM segments WHERE source_id = ? AND user_id = ? AND extracted_at IS NULL ORDER BY id",
        (source_id, user_id),
    )
    return [_segment(r) for r in rows]


def recent_important_segments(conn: sqlite3.Connection, user_id: int, since: datetime) -> list[Segment]:
    """High-importance items whose source was ingested since `since` (for the digest)."""
    rows = conn.execute(
        """SELECT s.* FROM segments s JOIN sources src ON src.id = s.source_id
           WHERE s.user_id = ? AND s.importance = 'high' AND src.created_at >= ?
           ORDER BY s.observed_at DESC LIMIT 10""",
        (user_id, iso(since)),
    )
    return [_segment(r) for r in rows]


def get_event(conn: sqlite3.Connection, user_id: int, event_id: int) -> Event | None:
    row = conn.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user_id)).fetchone()
    return Event(**dict(row)) if row else None


def list_events(
    conn: sqlite3.Connection,
    user_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
    statuses: Sequence[EventStatus] = ("upcoming", "conflict"),
) -> list[Event]:
    sql = f"SELECT * FROM events WHERE user_id = ? AND status IN ({_placeholders(len(statuses))})"
    params: list[object] = [user_id, *statuses]
    if start is not None:
        sql += " AND due_at >= ?"
        params.append(iso(start))
    if end is not None:
        sql += " AND due_at <= ?"
        params.append(iso(end))
    rows = conn.execute(sql + " ORDER BY due_at", params)
    return [Event(**dict(r)) for r in rows]


def set_event_status(conn: sqlite3.Connection, user_id: int, event_id: int, status: EventStatus) -> bool:
    cur = conn.execute(
        "UPDATE events SET status = ? WHERE id = ? AND user_id = ?", (status, event_id, user_id)
    )
    return cur.rowcount == 1


def mark_past_events(conn: sqlite3.Connection, user_id: int, now: datetime | None = None) -> int:
    cur = conn.execute(
        """UPDATE events SET status = 'past'
           WHERE user_id = ? AND status IN ('upcoming','conflict') AND valid_until < ?""",
        (user_id, iso(now or utcnow())),
    )
    return cur.rowcount


# ---------- reminder deliveries ----------

def claim_reminder(conn: sqlite3.Connection, user_id: int, event_id: int, kind: str) -> bool:
    """Record a delivery BEFORE sending. False if already sent for the event's current
    version (or not the user's event). A date change bumps the version, re-arming reminders."""
    cur = conn.execute(
        """INSERT INTO reminder_deliveries (event_id, event_version, kind, sent_at)
           SELECT id, version, ?, ? FROM events WHERE id = ? AND user_id = ?
           ON CONFLICT(event_id, event_version, kind) DO NOTHING""",
        (kind, iso(utcnow()), event_id, user_id),
    )
    return cur.rowcount == 1


def release_reminder(conn: sqlite3.Connection, user_id: int, event_id: int, kind: str) -> None:
    """Undo a claim after a failed send, so the next run retries it."""
    conn.execute(
        """DELETE FROM reminder_deliveries WHERE kind = ? AND (event_id, event_version) =
           (SELECT id, version FROM events WHERE id = ? AND user_id = ?)""",
        (kind, event_id, user_id),
    )


def reminders_sent(conn: sqlite3.Connection, user_id: int, event_ids: Sequence[int]) -> dict[int, list[str]]:
    """event_id -> reminder kinds already sent for its current version."""
    if not event_ids:
        return {}
    out: dict[int, list[str]] = {}
    for r in conn.execute(
        f"""SELECT rd.event_id, rd.kind FROM reminder_deliveries rd JOIN events e ON e.id = rd.event_id
            WHERE e.user_id = ? AND rd.event_version = e.version AND e.id IN ({_placeholders(len(event_ids))})""",
        (user_id, *event_ids),
    ):
        out.setdefault(r["event_id"], []).append(r["kind"])
    return out


# ---------- jobs (worker) ----------

def claim_job(conn: sqlite3.Connection, now: datetime | None = None) -> Job | None:
    """[system] Atomically claim the oldest pending job, or a stale processing one.

    Jobs locked longer than JOB_LOCK_TIMEOUT are reclaimed; if they already
    used all attempts they are marked failed instead.
    """
    now = now or utcnow()
    stale = iso(now - JOB_LOCK_TIMEOUT)
    with transaction(conn):
        conn.execute(
            """UPDATE jobs SET status = 'failed', locked_at = NULL,
                   last_error = COALESCE(last_error, 'lock expired after max attempts')
               WHERE status = 'processing' AND locked_at < ? AND attempts >= ?""",
            (stale, MAX_JOB_ATTEMPTS),
        )
        row = conn.execute(
            """UPDATE jobs SET status = 'processing', locked_at = ?, attempts = attempts + 1
               WHERE id = (
                   SELECT id FROM jobs
                   WHERE attempts < ? AND (status = 'pending' OR (status = 'processing' AND locked_at < ?))
                   ORDER BY created_at, id LIMIT 1)
               RETURNING *""",
            (iso(now), MAX_JOB_ATTEMPTS, stale),
        ).fetchone()
    return _job(row) if row else None


def requeue_interrupted_jobs(conn: sqlite3.Connection) -> int:
    """[system] At startup: jobs a previous (killed/restarted) process left 'processing'
    go straight back to pending instead of waiting out the 10-minute lock. The attempt
    they lost doesn't count. Only valid while a single worker process runs."""
    return conn.execute(
        """UPDATE jobs SET status = 'pending', locked_at = NULL, attempts = max(attempts - 1, 0)
           WHERE status = 'processing'"""
    ).rowcount


def complete_job(conn: sqlite3.Connection, job_id: int) -> None:
    """[system]"""
    conn.execute(
        "UPDATE jobs SET status = 'done', locked_at = NULL, last_error = NULL WHERE id = ?", (job_id,)
    )


def fail_job(conn: sqlite3.Connection, job_id: int, error: str) -> str:
    """[system] Back to pending for retry, or 'failed' after MAX_JOB_ATTEMPTS. Returns new status."""
    row = conn.execute(
        """UPDATE jobs SET locked_at = NULL, last_error = ?,
               status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END
           WHERE id = ? RETURNING status""",
        (error[:2000], MAX_JOB_ATTEMPTS, job_id),
    ).fetchone()
    return row[0] if row else "missing"


def get_job(conn: sqlite3.Connection, user_id: int, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
    return _job(row) if row else None


# ---------- login tokens ----------

def create_login_token(conn: sqlite3.Connection, user_id: int, ttl: timedelta = timedelta(minutes=15)) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO login_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, iso(utcnow() + ttl)),
    )
    return token


def consume_login_token(conn: sqlite3.Connection, token: str) -> int | None:
    """[system] One-time: deletes the token and returns its user_id if unexpired."""
    row = conn.execute(
        "DELETE FROM login_tokens WHERE token = ? RETURNING user_id, expires_at", (token,)
    ).fetchone()
    if row is None or row["expires_at"] < iso(utcnow()):
        return None
    return row["user_id"]


# ---------- spaces ----------

class SpaceNameTaken(ValueError):
    pass


def _space(row: sqlite3.Row | dict) -> Space:
    d = dict(row)
    d["rules"] = SpaceRules.model_validate(json.loads(d.pop("rules_json") or "{}"))
    d["pinned"] = bool(d["pinned"])
    return Space(**d)


def create_space(
    conn: sqlite3.Connection, user_id: int, name: str, kind: SpaceKind, *,
    auto_key: str | None = None, icon: str | None = None, color: str | None = None,
    rules: SpaceRules | None = None, pinned: bool = False,
) -> Space:
    name = name.strip()
    if not name:
        raise ValueError("space name is required")
    now = iso(utcnow())
    try:
        row = conn.execute(
            """INSERT INTO spaces (user_id, name, kind, auto_key, icon, color, rules_json, pinned,
                                   last_activity_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (user_id, name, kind, auto_key, icon, color,
             (rules or SpaceRules()).model_dump_json(exclude_defaults=True), int(pinned), now, now),
        ).fetchone()
    except sqlite3.IntegrityError as e:
        raise SpaceNameTaken(f"a space named {name!r} already exists") from e
    return _space(row)


def get_space(conn: sqlite3.Connection, user_id: int, space_id: int) -> Space | None:
    """A live (not deleted or merged) space of this user."""
    row = conn.execute(
        "SELECT * FROM spaces WHERE id = ? AND user_id = ? AND deleted_at IS NULL", (space_id, user_id)
    ).fetchone()
    return _space(row) if row else None


def get_space_by_key(conn: sqlite3.Connection, user_id: int, auto_key: str) -> Space | None:
    """Auto space for a key, following merges. None if missing or deleted by the user."""
    row = conn.execute("SELECT * FROM spaces WHERE user_id = ? AND auto_key = ?", (user_id, auto_key)).fetchone()
    for _ in range(10):  # follow merged_into chains
        if row is None:
            return None
        if row["deleted_at"] is None:
            return _space(row)
        if row["merged_into"] is None:
            return None
        row = conn.execute("SELECT * FROM spaces WHERE id = ? AND user_id = ?",
                           (row["merged_into"], user_id)).fetchone()
    return None


def auto_key_exists(conn: sqlite3.Connection, user_id: int, auto_key: str) -> bool:
    """True even for deleted or merged auto spaces, so they are never re-created."""
    return conn.execute("SELECT 1 FROM spaces WHERE user_id = ? AND auto_key = ?",
                        (user_id, auto_key)).fetchone() is not None


def list_spaces(conn: sqlite3.Connection, user_id: int) -> list[Space]:
    rows = conn.execute(
        """SELECT * FROM spaces WHERE user_id = ? AND deleted_at IS NULL
           ORDER BY pinned DESC, last_activity_at DESC, id""",
        (user_id,),
    )
    return [_space(r) for r in rows]


def list_rule_spaces(conn: sqlite3.Connection, user_id: int) -> list[Space]:
    return [s for s in list_spaces(conn, user_id) if not s.rules.is_empty()]


def list_spaces_overview(
    conn: sqlite3.Connection, user_id: int, now: datetime | None = None,
    event_filter: Callable[[Space, dict], bool] | None = None,
) -> list[dict]:
    """Live spaces with item counts and the next upcoming event; pinned first, then recent activity.

    `event_filter(space, event)` can reject events that merely share a segment with the
    space (e.g. a circular listing every course's exam, shown under one subject space).
    """
    now_iso = iso(now or utcnow())
    rows = conn.execute(
        """SELECT sp.*, (SELECT count(*) FROM space_items si WHERE si.space_id = sp.id) AS item_count
           FROM spaces sp WHERE sp.user_id = ? AND sp.deleted_at IS NULL
           ORDER BY sp.pinned DESC, sp.last_activity_at DESC, sp.id""",
        (user_id,),
    ).fetchall()
    candidates: dict[int, list[dict]] = {}
    for r in conn.execute(
        """SELECT si.space_id, e.id, e.title, e.type, e.subject, e.due_at, e.status
           FROM space_items si JOIN events e ON e.segment_id = si.segment_id
           JOIN spaces sp ON sp.id = si.space_id
           WHERE sp.user_id = ? AND e.user_id = ? AND e.status IN ('upcoming','conflict') AND e.due_at >= ?
           ORDER BY e.due_at""",
        (user_id, user_id, now_iso),
    ):
        candidates.setdefault(r["space_id"], []).append(dict(r))
    out = []
    for r in rows:
        d = dict(r)
        item_count = d.pop("item_count")
        space = _space(d)
        ev = next((e for e in candidates.get(space.id, []) if event_filter is None or event_filter(space, e)), None)
        out.append({**space.model_dump(), "item_count": item_count,
                    "next_event": {k: ev[k] for k in ("id", "title", "type", "due_at", "status")} if ev else None})
    return out


def update_space(
    conn: sqlite3.Connection, user_id: int, space_id: int, *, name: str | None = None,
    icon: str | None = None, color: str | None = None, pinned: bool | None = None,
    rules: SpaceRules | None = None,
) -> Space | None:
    if get_space(conn, user_id, space_id) is None:
        return None
    sets: list[str] = []
    params: list[object] = []
    for col, val in (("name", name.strip() if name is not None else None), ("icon", icon), ("color", color)):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if pinned is not None:
        sets.append("pinned = ?")
        params.append(int(pinned))
    if rules is not None:
        sets.append("rules_json = ?")
        params.append(rules.model_dump_json(exclude_defaults=True))
    if sets:
        try:
            conn.execute(f"UPDATE spaces SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
                         (*params, space_id, user_id))
        except sqlite3.IntegrityError as e:
            raise SpaceNameTaken(f"a space named {name!r} already exists") from e
    return get_space(conn, user_id, space_id)


def _retire_space(conn: sqlite3.Connection, user_id: int, space_id: int, merged_into: int | None) -> None:
    """Soft delete: frees the name, drops items, keeps the row so auto spaces are not re-created."""
    conn.execute(
        """UPDATE spaces SET deleted_at = ?, merged_into = ?, pinned = 0, name = name || ' #' || id || ' (removed)'
           WHERE id = ? AND user_id = ?""",
        (iso(utcnow()), merged_into, space_id, user_id),
    )
    conn.execute("DELETE FROM space_items WHERE space_id = ?", (space_id,))
    conn.execute("DELETE FROM jobs WHERE space_id = ? AND status IN ('pending','failed')", (space_id,))


def delete_space(conn: sqlite3.Connection, user_id: int, space_id: int) -> bool:
    """Delete a space. The segments themselves are never touched."""
    space = get_space(conn, user_id, space_id)
    if space is None:
        return False
    with transaction(conn):
        if space.kind == "auto":
            _retire_space(conn, user_id, space_id, None)
        else:
            conn.execute("DELETE FROM spaces WHERE id = ? AND user_id = ?", (space_id, user_id))
    return True


def merge_spaces(conn: sqlite3.Connection, user_id: int, space_id: int, into_id: int) -> Space | None:
    """Move every item of space_id into into_id and retire the former.

    Future auto items for the merged space's subject go to the target.
    """
    if space_id == into_id:
        raise ValueError("cannot merge a space into itself")
    src, dst = get_space(conn, user_id, space_id), get_space(conn, user_id, into_id)
    if src is None or dst is None:
        return None
    with transaction(conn):
        conn.execute(
            """INSERT INTO space_items (space_id, segment_id, added_by, score, added_at)
               SELECT ?, si.segment_id, si.added_by, si.score, si.added_at FROM space_items si
               WHERE si.space_id = ?
                 AND si.segment_id NOT IN (SELECT segment_id FROM space_exclusions WHERE space_id = ?)
               ON CONFLICT(space_id, segment_id) DO NOTHING""",
            (into_id, space_id, into_id),
        )
        conn.execute("UPDATE spaces SET last_activity_at = ? WHERE id = ? AND user_id = ?",
                     (iso(utcnow()), into_id, user_id))
        _retire_space(conn, user_id, space_id, into_id)
    return get_space(conn, user_id, into_id)


def add_space_items(
    conn: sqlite3.Connection, user_id: int, space_id: int, items: Sequence[tuple[int, AddedBy, float]],
) -> int:
    """Add (segment_id, added_by, score) items. Excluded segments are skipped unless added by
    the user, which lifts the exclusion. Returns how many were newly added."""
    if not items or get_space(conn, user_id, space_id) is None:
        return 0
    ids = list({seg for seg, _, _ in items})
    owned: set[int] = set()
    for i in range(0, len(ids), 500):
        batch = ids[i:i + 500]
        owned |= {r[0] for r in conn.execute(
            f"SELECT id FROM segments WHERE user_id = ? AND id IN ({_placeholders(len(batch))})", (user_id, *batch))}
    excluded = {r[0] for r in conn.execute("SELECT segment_id FROM space_exclusions WHERE space_id = ?", (space_id,))}
    now = iso(utcnow())
    added = 0
    with transaction(conn):
        for seg, by, score in items:
            if seg not in owned:
                continue
            if seg in excluded:
                if by != "user":
                    continue
                conn.execute("DELETE FROM space_exclusions WHERE space_id = ? AND segment_id = ?", (space_id, seg))
            exists = conn.execute("SELECT 1 FROM space_items WHERE space_id = ? AND segment_id = ?",
                                  (space_id, seg)).fetchone()
            if exists:
                if by == "user":
                    conn.execute("UPDATE space_items SET added_by = 'user' WHERE space_id = ? AND segment_id = ?",
                                 (space_id, seg))
                continue
            conn.execute(
                "INSERT INTO space_items (space_id, segment_id, added_by, score, added_at) VALUES (?, ?, ?, ?, ?)",
                (space_id, seg, by, score, now),
            )
            added += 1
        if added:
            conn.execute("UPDATE spaces SET last_activity_at = ? WHERE id = ?", (now, space_id))
    return added


def remove_space_item(conn: sqlite3.Connection, user_id: int, space_id: int, segment_id: int) -> bool:
    """Remove an item and remember it, so rules and auto never add it back."""
    if get_space(conn, user_id, space_id) is None or get_segment(conn, user_id, segment_id) is None:
        return False
    with transaction(conn):
        conn.execute("DELETE FROM space_items WHERE space_id = ? AND segment_id = ?", (space_id, segment_id))
        conn.execute(
            """INSERT INTO space_exclusions (space_id, segment_id, created_at) VALUES (?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (space_id, segment_id, iso(utcnow())),
        )
    return True


def clear_rule_items(conn: sqlite3.Connection, user_id: int, space_id: int) -> None:
    if get_space(conn, user_id, space_id) is not None:
        conn.execute("DELETE FROM space_items WHERE space_id = ? AND added_by = 'rule'", (space_id,))


def space_segment_ids(conn: sqlite3.Connection, user_id: int, space_id: int) -> set[int]:
    return {r[0] for r in conn.execute(
        """SELECT si.segment_id FROM space_items si JOIN spaces sp ON sp.id = si.space_id
           WHERE si.space_id = ? AND sp.user_id = ?""",
        (space_id, user_id),
    )}


def spaces_of_segments(conn: sqlite3.Connection, user_id: int, segment_ids: Sequence[int]) -> dict[int, list[dict]]:
    """segment_id -> [{id, name, icon, color}] for showing which spaces a card is in."""
    if not segment_ids:
        return {}
    out: dict[int, list[dict]] = {}
    for r in conn.execute(
        f"""SELECT si.segment_id, sp.id, sp.name, sp.icon, sp.color FROM space_items si
            JOIN spaces sp ON sp.id = si.space_id
            WHERE sp.user_id = ? AND sp.deleted_at IS NULL AND si.segment_id IN ({_placeholders(len(segment_ids))})
            ORDER BY sp.kind DESC, sp.name""",
        (user_id, *segment_ids),
    ):
        out.setdefault(r["segment_id"], []).append({"id": r["id"], "name": r["name"], "icon": r["icon"],
                                                    "color": r["color"]})
    return out


def _encode_cursor(observed_at: str, segment_id: int) -> str:
    return base64.urlsafe_b64encode(f"{observed_at}|{segment_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        ts, seg = base64.urlsafe_b64decode(cursor.encode()).decode().rsplit("|", 1)
        return ts, int(seg)
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError("invalid cursor") from e


def list_space_items(
    conn: sqlite3.Connection, user_id: int, space_id: int, *, cursor: str | None = None, limit: int = 20,
    category: str | None = None, source_type: SourceType | None = None,
) -> tuple[list[dict], str | None]:
    """Items newest first (by observed_at), keyset-paginated. Returns (items, next_cursor)."""
    sql = """SELECT s.id AS segment_id, s.text, s.summary, s.category, s.subject, s.importance, s.observed_at,
                    s.locator_json, si.added_by, si.score, si.added_at,
                    src.id AS source_id, src.type AS source_type, src.title AS source_title
             FROM space_items si
             JOIN spaces sp ON sp.id = si.space_id
             JOIN segments s ON s.id = si.segment_id
             JOIN sources src ON src.id = s.source_id
             WHERE si.space_id = ? AND sp.user_id = ? AND s.user_id = ?"""
    params: list[object] = [space_id, user_id, user_id]
    if category:
        sql += " AND (s.category = ? OR EXISTS (SELECT 1 FROM events e WHERE e.segment_id = s.id AND e.type = ?))"
        params += [category, category]
    if source_type:
        sql += " AND src.type = ?"
        params.append(source_type)
    if cursor:
        ts, seg = _decode_cursor(cursor)
        sql += " AND (s.observed_at < ? OR (s.observed_at = ? AND s.id < ?))"
        params += [ts, ts, seg]
    rows = conn.execute(sql + " ORDER BY s.observed_at DESC, s.id DESC LIMIT ?", (*params, limit + 1)).fetchall()
    items = []
    for r in rows[:limit]:
        d = dict(r)
        d["locator"] = json.loads(d.pop("locator_json") or "{}")
        items.append(d)
    nxt = _encode_cursor(rows[limit - 1]["observed_at"], rows[limit - 1]["segment_id"]) if len(rows) > limit else None
    return items, nxt


# ---------- segment subjects + contexts (for space assignment) ----------

def set_segment_subjects(conn: sqlite3.Connection, user_id: int, segment_id: int,
                         subjects: Sequence[tuple[str, str]]) -> None:
    """Replace the normalized (subject_key, subject_name) pairs of a segment."""
    if get_segment(conn, user_id, segment_id) is None:
        raise PermissionError(f"segment {segment_id} not owned by user {user_id}")
    with transaction(conn):
        conn.execute("DELETE FROM segment_subjects WHERE segment_id = ? AND user_id = ?", (segment_id, user_id))
        conn.executemany(
            "INSERT OR IGNORE INTO segment_subjects (segment_id, user_id, subject_key, subject_name) VALUES (?, ?, ?, ?)",
            [(segment_id, user_id, k, n) for k, n in subjects],
        )


def segments_with_subject(conn: sqlite3.Connection, user_id: int, subject_key: str) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT segment_id FROM segment_subjects WHERE user_id = ? AND subject_key = ? ORDER BY segment_id",
        (user_id, subject_key),
    )]


def known_subject_names(conn: sqlite3.Connection, user_id: int) -> list[str]:
    names = {r[0] for r in conn.execute("SELECT DISTINCT subject_name FROM segment_subjects WHERE user_id = ?",
                                        (user_id,))}
    names |= {r[0] for r in conn.execute(
        "SELECT name FROM spaces WHERE user_id = ? AND auto_key LIKE 'subject:%' AND deleted_at IS NULL", (user_id,))}
    return sorted(names)


def segment_contexts(conn: sqlite3.Connection, user_id: int,
                     segment_ids: Sequence[int] | None = None) -> list[SegmentContext]:
    """Segments joined with source info and their events; all of the user's when ids is None."""
    sql = """SELECT s.id AS segment_id, s.text, s.summary, s.category, s.subject, s.observed_at, s.locator_json,
                    src.type AS source_type, src.title AS source_title
             FROM segments s JOIN sources src ON src.id = s.source_id WHERE s.user_id = ?"""
    params: list[object] = [user_id]
    if segment_ids is not None:
        if not segment_ids:
            return []
        sql += f" AND s.id IN ({_placeholders(len(segment_ids))})"
        params += list(segment_ids)
    rows = conn.execute(sql + " ORDER BY s.id", params).fetchall()
    events: dict[int, list[tuple[str, str | None]]] = {}
    ids = [r["segment_id"] for r in rows]
    for i in range(0, len(ids), 500):
        batch = ids[i:i + 500]
        for e in conn.execute(
            f"""SELECT segment_id, type, subject, title FROM events
                WHERE user_id = ? AND segment_id IN ({_placeholders(len(batch))})""",
            (user_id, *batch),
        ):
            events.setdefault(e["segment_id"], []).append((e["type"], effective_subject(e["subject"], e["title"])))
    out = []
    for r in rows:
        d = dict(r)
        d["locator"] = json.loads(d.pop("locator_json") or "{}")
        evs = events.get(d["segment_id"], [])
        out.append(SegmentContext(**d, event_types=[t for t, _ in evs], event_subjects=[s for _, s in evs if s]))
    return out


def min_segment_distance(conn: sqlite3.Connection, user_id: int, segment_id: int,
                         embedding: Sequence[float]) -> float | None:
    """Smallest cosine distance between a query vector and any chunk of the segment."""
    row = conn.execute(
        """SELECT min(vec_distance_cosine(v.embedding, ?)) FROM chunks_vec v
           JOIN chunks c ON c.id = v.chunk_id WHERE c.segment_id = ? AND c.user_id = ?""",
        (sqlite_vec.serialize_float32(list(embedding)), segment_id, user_id),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def enqueue_job(conn: sqlite3.Connection, user_id: int, kind: str, *, space_id: int | None = None,
                payload: dict | None = None) -> int:
    """Queue a non-source job (e.g. a space backfill); reuses an identical pending one."""
    row = conn.execute(
        "SELECT id FROM jobs WHERE user_id = ? AND kind = ? AND space_id IS ? AND status = 'pending'",
        (user_id, kind, space_id),
    ).fetchone()
    if row:
        return row[0]
    return conn.execute(
        """INSERT INTO jobs (user_id, space_id, kind, status, created_at, payload_json)
           VALUES (?, ?, ?, 'pending', ?, ?) RETURNING id""",
        (user_id, space_id, kind, iso(utcnow()), json.dumps(payload or {})),
    ).fetchone()[0]


# ---------- calendar ----------

CALENDAR_STATUSES: tuple[EventStatus, ...] = ("upcoming", "conflict", "done", "past")
EDITABLE_STATUSES = ("upcoming", "done", "cancelled")


def list_events_range(
    conn: sqlite3.Connection, user_id: int, start: datetime, end: datetime, *,
    space_id: int | None = None, types: Sequence[str] | None = None,
    statuses: Sequence[EventStatus] = CALENDAR_STATUSES,
) -> list[Event]:
    """Events overlapping [start, end] (UTC compare), optionally only those in a space."""
    sql = f"""SELECT e.* FROM events e WHERE e.user_id = ? AND e.status IN ({_placeholders(len(statuses))})
              AND e.due_at <= ? AND COALESCE(e.end_at, e.due_at) >= ?"""
    params: list[object] = [user_id, *statuses, iso(end), iso(start)]
    if types:
        sql += f" AND e.type IN ({_placeholders(len(types))})"
        params += list(types)
    if space_id is not None:
        sql += """ AND e.segment_id IN (SELECT si.segment_id FROM space_items si JOIN spaces sp ON sp.id = si.space_id
                                        WHERE si.space_id = ? AND sp.user_id = ?)"""
        params += [space_id, user_id]
    return [Event(**dict(r)) for r in conn.execute(sql + " ORDER BY e.due_at, e.id", params)]


def create_event(
    conn: sqlite3.Connection, user_id: int, *, title: str, type: str, due_at: datetime,
    end_at: datetime | None = None, all_day: bool = False, location: str | None = None,
    subject: str | None = None, segment_id: int | None = None,
) -> Event:
    """A manual event (user_edited, never touched by extraction)."""
    if segment_id is not None and get_segment(conn, user_id, segment_id) is None:
        raise PermissionError(f"segment {segment_id} not owned by user {user_id}")
    if end_at is not None and end_at < due_at:
        raise ValueError("end must be after start")
    now = iso(utcnow())
    last = end_at or due_at
    row = conn.execute(
        """INSERT INTO events (user_id, segment_id, title, type, subject, due_at, end_at, all_day, location,
                               confidence, status, valid_until, user_edited, version, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, 1, 1, ?, ?) RETURNING *""",
        (user_id, segment_id, title.strip(), type, subject, iso(due_at), iso(end_at) if end_at else None,
         int(all_day), location, "past" if last + timedelta(days=1) < utcnow() else "upcoming",
         iso(last + timedelta(days=1)), now, now),
    ).fetchone()
    return Event(**dict(row))


def update_event(conn: sqlite3.Connection, user_id: int, event_id: int, **fields: object) -> Event | None:
    """Edit an event. Marks it user_edited. Changing the date/time bumps `version`
    (reminders are re-sent for the new date) and recomputes valid_until."""
    e = get_event(conn, user_id, event_id)
    if e is None:
        return None
    allowed = {"title", "type", "subject", "due_at", "end_at", "all_day", "location", "status"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot edit {sorted(unknown)}")
    if "status" in fields and fields["status"] not in EDITABLE_STATUSES:
        raise ValueError(f"status must be one of {EDITABLE_STATUSES}")
    sets: dict[str, object] = {}
    for key, value in fields.items():
        if key in ("due_at", "end_at") and isinstance(value, datetime):
            value = iso(value)
        if key == "all_day":
            value = int(bool(value))
        sets[key] = value
    due = sets.get("due_at", e.due_at)
    end = sets.get("end_at", e.end_at)
    if end is not None and end < due:
        raise ValueError("end must be after start")
    date_changed = due != e.due_at or end != e.end_at or int(sets.get("all_day", int(e.all_day))) != int(e.all_day)
    now = utcnow()
    if date_changed:
        last = datetime.fromisoformat(end or due)
        sets["valid_until"] = iso(last + timedelta(days=1))
        sets["version"] = e.version + 1
        if "status" not in fields and e.status == "past" and last + timedelta(days=1) >= now:
            sets["status"] = "upcoming"
    sets["user_edited"] = 1
    sets["updated_at"] = iso(now)
    cols = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE events SET {cols} WHERE id = ? AND user_id = ?", (*sets.values(), event_id, user_id))
    return get_event(conn, user_id, event_id)


def delete_event(conn: sqlite3.Connection, user_id: int, event_id: int) -> bool:
    return conn.execute("DELETE FROM events WHERE id = ? AND user_id = ?", (event_id, user_id)).rowcount == 1


def conflict_partners(conn: sqlite3.Connection, user_id: int, event_id: int) -> list[Event]:
    """Other conflict-status events that clash with this one."""
    e = conn.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (event_id, user_id)).fetchone()
    if e is None:
        return []
    rows = conn.execute("SELECT * FROM events WHERE user_id = ? AND type = ? AND status = 'conflict' AND id <> ?",
                        (user_id, e["type"], event_id)).fetchall()
    return [Event(**dict(r)) for r in rows if _clashes(dict(r), dict(e))]


def resolve_conflict(conn: sqlite3.Connection, user_id: int, event_id: int, action: str) -> list[Event]:
    """'update': this event is the current date; its clashing partners are superseded.
    'keep_both': they are different events; all go back to 'upcoming'.
    Returns the affected events (this one first)."""
    e = get_event(conn, user_id, event_id)
    if e is None:
        return []
    partners = conflict_partners(conn, user_id, event_id)
    with transaction(conn):
        if action == "update":
            for p in partners:
                _supersede(conn, user_id, p.id, event_id)
            conn.execute("UPDATE events SET status = 'upcoming', user_edited = 1 WHERE id = ? AND user_id = ?",
                         (event_id, user_id))
        elif action == "keep_both":
            ids = [event_id, *[p.id for p in partners]]
            conn.execute(f"UPDATE events SET status = 'upcoming', user_edited = 1 WHERE user_id = ? AND id IN "
                         f"({_placeholders(len(ids))})", (user_id, *ids))
        else:
            raise ValueError("action must be 'update' or 'keep_both'")
    return [ev for ev in (get_event(conn, user_id, i) for i in [event_id, *[p.id for p in partners]]) if ev]


def get_or_create_ical_token(conn: sqlite3.Connection, user_id: int) -> str:
    row = conn.execute("SELECT ical_token FROM users WHERE id = ?", (user_id,)).fetchone()
    if row and row[0]:
        return row[0]
    return regenerate_ical_token(conn, user_id)


def regenerate_ical_token(conn: sqlite3.Connection, user_id: int) -> str:
    """New secret for the calendar feed; the old subscription URL stops working."""
    token = secrets.token_urlsafe(24)
    conn.execute("UPDATE users SET ical_token = ? WHERE id = ?", (token, user_id))
    return token


def user_by_ical_token(conn: sqlite3.Connection, token: str) -> User | None:
    """[system] The calendar feed authenticates with its own per-user secret."""
    if not token:
        return None
    row = conn.execute("SELECT * FROM users WHERE ical_token = ?", (token,)).fetchone()
    return User(**dict(row)) if row else None


def segment_brief(conn: sqlite3.Connection, user_id: int, segment_ids: Sequence[int]) -> dict[int, dict]:
    """segment_id -> {text, observed_at, locator, source{id,type,title}} for event panels."""
    if not segment_ids:
        return {}
    rows = conn.execute(
        f"""SELECT s.id, s.text, s.observed_at, s.locator_json, s.importance, s.category,
                   src.id AS source_id, src.type AS source_type, src.title AS source_title
            FROM segments s JOIN sources src ON src.id = s.source_id
            WHERE s.user_id = ? AND s.id IN ({_placeholders(len(segment_ids))})""",
        (user_id, *segment_ids),
    )
    return {r["id"]: {"segment_id": r["id"], "text": r["text"], "observed_at": r["observed_at"],
                      "locator": json.loads(r["locator_json"] or "{}"), "importance": r["importance"],
                      "category": r["category"],
                      "source": {"id": r["source_id"], "type": r["source_type"], "title": r["source_title"]}}
            for r in rows}


# ---------- Today feed ----------

def set_last_seen_today(conn: sqlite3.Connection, user_id: int, when: datetime) -> None:
    conn.execute("UPDATE users SET last_seen_today_at = ? WHERE id = ?", (iso(when), user_id))


def snooze(conn: sqlite3.Connection, user_id: int, item_kind: str, item_id: int, until: datetime) -> bool:
    """Hide an event or segment from Today until `until`. False if it isn't the user's."""
    table = {"event": "events", "segment": "segments"}.get(item_kind)
    if table is None:
        raise ValueError("item_kind must be 'event' or 'segment'")
    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ? AND user_id = ?", (item_id, user_id)).fetchone() is None:
        return False
    conn.execute(
        """INSERT INTO snoozes (user_id, item_kind, item_id, until) VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, item_kind, item_id) DO UPDATE SET until = excluded.until""",
        (user_id, item_kind, item_id, iso(until)),
    )
    return True


def active_snoozes(conn: sqlite3.Connection, user_id: int, now: datetime) -> set[tuple[str, int]]:
    return {(r[0], r[1]) for r in conn.execute(
        "SELECT item_kind, item_id FROM snoozes WHERE user_id = ? AND until > ?", (user_id, iso(now)))}


def mark_segment_opened(conn: sqlite3.Connection, user_id: int, segment_id: int, when: datetime | None = None) -> bool:
    cur = conn.execute("UPDATE segments SET opened_at = ? WHERE id = ? AND user_id = ?",
                       (iso(when or utcnow()), segment_id, user_id))
    return cur.rowcount == 1


def _segment_cards(rows: Iterable[sqlite3.Row]) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r)
        d["locator"] = json.loads(d.pop("locator_json") or "{}")
        d["source"] = {"id": d.pop("source_id"), "type": d.pop("source_type"), "title": d.pop("source_title")}
        out.append(d)
    return out


_CARD_SQL = """SELECT s.id AS segment_id, s.text, s.summary, s.category, s.subject, s.importance, s.observed_at,
                      s.opened_at, s.locator_json, src.created_at AS added_at,
                      src.id AS source_id, src.type AS source_type, src.title AS source_title
               FROM segments s JOIN sources src ON src.id = s.source_id"""


def segments_added_since(conn: sqlite3.Connection, user_id: int, since: datetime,
                         importances: Sequence[str] = ("high", "medium"), limit: int = 50) -> list[dict]:
    """Items whose source was added after `since`, most important then newest first."""
    rows = conn.execute(
        f"""{_CARD_SQL} WHERE s.user_id = ? AND src.created_at > ? AND s.importance IN ({_placeholders(len(importances))})
            ORDER BY CASE s.importance WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, s.observed_at DESC
            LIMIT ?""",
        (user_id, iso(since), *importances, limit),
    )
    return _segment_cards(rows)


def rediscover_candidates(conn: sqlite3.Connection, user_id: int, older_than: datetime, limit: int = 20) -> list[dict]:
    """High-importance resources saved before `older_than` and never opened."""
    rows = conn.execute(
        f"""{_CARD_SQL} WHERE s.user_id = ? AND s.category = 'resource' AND s.importance = 'high'
            AND s.opened_at IS NULL AND src.created_at < ? ORDER BY s.observed_at LIMIT ?""",
        (user_id, iso(older_than), limit),
    )
    return _segment_cards(rows)


def failed_jobs(conn: sqlite3.Connection, user_id: int, limit: int = 20) -> list[dict]:
    """Sources whose latest processing job failed."""
    rows = conn.execute(
        """SELECT j.id AS job_id, j.kind, j.last_error, j.created_at, src.id AS source_id, src.type AS source_type,
                  src.title AS source_title
           FROM jobs j JOIN sources src ON src.id = j.source_id
           WHERE j.user_id = ? AND src.user_id = ? AND j.status = 'failed'
             AND j.id = (SELECT max(id) FROM jobs j2 WHERE j2.source_id = j.source_id)
           ORDER BY j.created_at DESC LIMIT ?""",
        (user_id, user_id, limit),
    )
    return [dict(r) for r in rows]


def clear_user_index(conn: sqlite3.Connection, user_id: int) -> None:
    """Drop every chunk, FTS row and vector of a user (segments are kept) before a reindex."""
    with transaction(conn):
        ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE user_id = ?", (user_id,))]
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({_placeholders(len(batch))})", batch)
            conn.execute(f"DELETE FROM chunks_vec WHERE chunk_id IN ({_placeholders(len(batch))})", batch)
        conn.execute("DELETE FROM chunks WHERE user_id = ?", (user_id,))
