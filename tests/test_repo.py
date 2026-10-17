from datetime import datetime, timedelta, timezone

import pytest

from app import repo
from app.models import ChunkIn, EventIn, SegmentIn
from tests.conftest import IST, seg, unit_vec, utc


# ---------- users ----------

def test_upsert_user_idempotent(conn):
    u1 = repo.upsert_user(conn, 555, "Asha")
    u2 = repo.upsert_user(conn, 555, None)
    assert u1.id == u2.id and u2.name == "Asha" and u2.timezone == "Asia/Kolkata"


# ---------- ingestion transaction + dedup ----------

def test_ingest_creates_source_segments_and_job(conn, users):
    a, _ = users
    res = repo.ingest_source(conn, a, "telegram", repo.content_hash("DBMS exam on 20 Oct"),
                             [seg("DBMS exam on 20 Oct", chat_id=1, message_id=7)])
    assert res.created and res.job_id
    segs = repo.list_segments(conn, a, res.source_id)
    assert len(segs) == 1 and segs[0].locator == {"chat_id": 1, "message_id": 7}
    assert segs[0].observed_at == "2026-10-12T15:45:00+00:00"  # IST normalized to UTC
    assert repo.get_job(conn, a, res.job_id).status == "pending"


def test_ingest_dedup_by_content_hash(conn, users):
    a, b = users
    h = repo.content_hash("same  text")
    first = repo.ingest_source(conn, a, "telegram", h, [seg("same text")])
    dup = repo.ingest_source(conn, a, "telegram", repo.content_hash("same text"), [seg("same text")])
    assert not dup.created and dup.source_id == first.source_id and dup.job_id is None
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM segments").fetchone()[0] == 1
    # Dedup is per user: Bob can store the same content.
    assert repo.ingest_source(conn, b, "telegram", h, [seg("same text")]).created


def test_ingest_rolls_back_on_failure(conn, users):
    a, _ = users
    with pytest.raises(Exception):
        repo.ingest_source(conn, a, "fax", "h1", [seg("x")])  # type: ignore[arg-type]
    for t in ("sources", "segments", "jobs"):
        assert conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0


def test_naive_datetime_rejected(conn, users):
    a, _ = users
    bad = seg("x")
    bad.observed_at = datetime(2026, 1, 1)
    with pytest.raises(ValueError):
        repo.ingest_source(conn, a, "telegram", "h", [bad])
    assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 0


# ---------- chunks + FTS + vectors ----------

def _source_with_chunks(conn, user_id, text="Operating Systems midsem on Friday", hot=(0,)):
    res = repo.ingest_source(conn, user_id, "telegram", repo.content_hash(text), [seg(text)])
    seg_id = repo.list_segments(conn, user_id, res.source_id)[0].id
    ids = repo.add_chunks(conn, user_id, seg_id, [ChunkIn(text=text, char_start=0, char_end=len(text))],
                          [unit_vec(*hot)])
    return res.source_id, seg_id, ids[0]


def test_fts_porter_and_vector_search(conn, users):
    a, _ = users
    _, _, cid = _source_with_chunks(conn, a, "Assignments are due on Monday", hot=(1,))
    _source_with_chunks(conn, a, "Canteen menu changed", hot=(2,))
    # porter: "assignment" matches "Assignments"
    assert [c for c, _ in repo.fts_search(conn, a, '"assignment"')] == [cid]
    hits = repo.vec_search(conn, a, unit_vec(1), limit=5)
    assert hits[0][0] == cid and hits[0][1] == pytest.approx(0.0, abs=1e-6)
    assert hits[1][1] == pytest.approx(1.0, abs=1e-6)  # orthogonal => cosine distance 1
    hydrated = repo.get_chunk_hits(conn, a, [cid])[cid]
    assert hydrated.source_type == "telegram" and "Monday" in hydrated.text


def test_add_chunks_atomic_on_bad_embedding(conn, users):
    a, _ = users
    res = repo.ingest_source(conn, a, "telegram", "h", [seg("abc def")])
    seg_id = repo.list_segments(conn, a, res.source_id)[0].id
    with pytest.raises(Exception):
        repo.add_chunks(conn, a, seg_id, [ChunkIn(text="abc", char_start=0, char_end=3)], [[0.1, 0.2]])
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 0


# ---------- delete cascade ----------

def test_delete_source_cascades_everything(conn, users, tmp_path):
    a, _ = users
    f = tmp_path / "circular.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    res = repo.ingest_source(conn, a, "upload", repo.content_hash(b"%PDF-1.4 fake"),
                             [seg("Page 1: Exam schedule", page=1)], file_path=str(f), title="circular.pdf")
    seg_id = repo.list_segments(conn, a, res.source_id)[0].id
    repo.add_chunks(conn, a, seg_id, [ChunkIn(text="Exam schedule", char_start=8, char_end=21)], [unit_vec(3)])
    ev = repo.add_event(conn, a, EventIn(segment_id=seg_id, title="DBMS exam", type="exam", subject="DBMS",
                                         due_at=datetime(2026, 10, 20, 10, tzinfo=IST), confidence=0.9))
    assert repo.claim_reminder(conn, a, ev, "24h")

    assert repo.delete_source(conn, a, res.source_id)

    for t in ("sources", "segments", "chunks", "chunks_fts", "chunks_vec", "events", "jobs",
              "reminder_deliveries"):
        assert conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, t
    assert not f.exists()
    assert not repo.delete_source(conn, a, res.source_id)  # already gone


# ---------- events: lifecycle + conflicts ----------

def _segment(conn, user_id, text="notice"):
    res = repo.ingest_source(conn, user_id, "telegram", repo.content_hash(text), [seg(text)])
    return repo.list_segments(conn, user_id, res.source_id)[0].id


def test_event_valid_until_and_conflict(conn, users):
    a, _ = users
    s1, s2 = _segment(conn, a, "DBMS exam 20 Oct"), _segment(conn, a, "DBMS exam moved to 22 Oct")
    e1 = repo.add_event(conn, a, EventIn(segment_id=s1, title="DBMS midsem", type="exam", subject="DBMS",
                                         due_at=datetime(2026, 10, 20, 10, tzinfo=IST), confidence=0.9))
    ev = repo.list_events(conn, a)[0]
    assert ev.status == "upcoming" and ev.due_at == "2026-10-20T04:30:00+00:00"
    assert ev.valid_until == "2026-10-21T04:30:00+00:00"

    e2 = repo.add_event(conn, a, EventIn(segment_id=s2, title="DBMS midsem", type="exam", subject="dbms",
                                         due_at=datetime(2026, 10, 22, 10, tzinfo=IST), confidence=0.9))
    statuses = {e.id: e.status for e in repo.list_events(conn, a)}
    assert statuses == {e1: "conflict", e2: "conflict"}


def test_same_due_at_or_other_type_is_not_conflict(conn, users):
    a, _ = users
    s = _segment(conn, a)
    due = datetime(2026, 10, 20, 10, tzinfo=IST)
    repo.add_event(conn, a, EventIn(segment_id=s, title="OS exam", type="exam", subject="OS", due_at=due, confidence=0.8))
    repo.add_event(conn, a, EventIn(segment_id=s, title="OS exam", type="exam", subject="OS", due_at=due, confidence=0.8))
    repo.add_event(conn, a, EventIn(segment_id=s, title="OS lab file", type="deadline", subject="OS",
                                    due_at=due + timedelta(days=3), confidence=0.8))
    assert {e.status for e in repo.list_events(conn, a)} == {"upcoming"}


def test_mark_past_and_date_range(conn, users):
    a, _ = users
    s = _segment(conn, a)
    then = utc(2026, 9, 30)
    repo.add_event(conn, a, EventIn(segment_id=s, title="old", type="deadline", subject="X",
                                    due_at=utc(2026, 10, 1, 12), confidence=0.9), now=then)
    repo.add_event(conn, a, EventIn(segment_id=s, title="soon", type="deadline", subject="Y",
                                    due_at=utc(2026, 10, 18, 12), confidence=0.9), now=then)
    assert repo.mark_past_events(conn, a, now=utc(2026, 10, 17)) == 1
    upcoming = repo.list_events(conn, a, start=utc(2026, 10, 17), end=utc(2026, 10, 24))
    assert [e.title for e in upcoming] == ["soon"]


# ---------- reminders ----------

def test_reminder_claim_is_unique_and_releasable(conn, users):
    a, _ = users
    s = _segment(conn, a)
    ev = repo.add_event(conn, a, EventIn(segment_id=s, title="t", type="exam", subject="S",
                                         due_at=utc(2026, 10, 18), confidence=0.9))
    assert repo.claim_reminder(conn, a, ev, "24h")
    assert not repo.claim_reminder(conn, a, ev, "24h")
    assert repo.claim_reminder(conn, a, ev, "2h")
    repo.release_reminder(conn, a, ev, "24h")  # send failed -> retry allowed
    assert repo.claim_reminder(conn, a, ev, "24h")


# ---------- jobs ----------

def test_job_claim_retry_and_max_attempts(conn, users):
    a, _ = users
    jid = repo.ingest_source(conn, a, "telegram", "h", [seg("x")]).job_id
    for attempt in (1, 2, 3):
        job = repo.claim_job(conn)
        assert job.id == jid and job.attempts == attempt and job.status == "processing"
        assert repo.claim_job(conn) is None  # locked
        status = repo.fail_job(conn, jid, f"boom {attempt}")
    assert status == "failed"
    assert repo.claim_job(conn) is None
    assert repo.get_job(conn, a, jid).last_error == "boom 3"


def test_stale_job_is_reclaimed(conn, users):
    a, _ = users
    jid = repo.ingest_source(conn, a, "telegram", "h", [seg("x")]).job_id
    t0 = datetime.now(timezone.utc)
    assert repo.claim_job(conn, now=t0).id == jid
    assert repo.claim_job(conn, now=t0 + timedelta(minutes=5)) is None
    again = repo.claim_job(conn, now=t0 + timedelta(minutes=11))
    assert again.id == jid and again.attempts == 2
    repo.complete_job(conn, jid)
    assert repo.get_job(conn, a, jid).status == "done"


def test_stale_job_at_max_attempts_fails(conn, users):
    a, _ = users
    jid = repo.ingest_source(conn, a, "telegram", "h", [seg("x")]).job_id
    t = datetime.now(timezone.utc)
    for i in range(3):  # crash without fail_job three times
        assert repo.claim_job(conn, now=t + timedelta(minutes=11 * i)).id == jid
    assert repo.claim_job(conn, now=t + timedelta(minutes=50)) is None
    assert repo.get_job(conn, a, jid).status == "failed"


# ---------- login tokens ----------

def test_login_token_one_time_and_expiry(conn, users):
    a, _ = users
    tok = repo.create_login_token(conn, a)
    assert repo.consume_login_token(conn, tok) == a
    assert repo.consume_login_token(conn, tok) is None  # one-time
    expired = repo.create_login_token(conn, a, ttl=timedelta(seconds=-1))
    assert repo.consume_login_token(conn, expired) is None
    assert repo.consume_login_token(conn, "nonsense") is None


def test_event_already_over_is_stored_past(conn, users):
    a, _ = users
    s = _segment(conn, a)
    repo.add_event(conn, a, EventIn(segment_id=s, title="Quiz 1", type="exam", subject="CD",
                                    due_at=utc(2026, 9, 29, 4), confidence=0.9), now=utc(2026, 10, 17))
    assert repo.list_events(conn, a) == []
    assert repo.list_events(conn, a, statuses=("past",))[0].title == "Quiz 1"


def test_duplicate_announcement_is_not_a_second_event(conn, users):
    a, _ = users
    s1, s2 = _segment(conn, a, "a"), _segment(conn, a, "b")
    due = datetime(2026, 11, 21, 10, tzinfo=IST)
    e1 = repo.add_event(conn, a, EventIn(segment_id=s1, title="DBMS midsem", type="exam", subject="DBMS",
                                         due_at=due, confidence=0.9))
    e2 = repo.add_event(conn, a, EventIn(segment_id=s2, title="Database Management Systems midsem", type="exam",
                                         subject="CS301 Database Management Systems", due_at=due, confidence=0.9))
    assert e1 == e2 and len(repo.list_events(conn, a)) == 1


def test_conflict_needs_same_assessment(conn, users):
    a, _ = users
    s = _segment(conn, a)
    def add(title, day, subject="CN"):
        return repo.add_event(conn, a, EventIn(segment_id=s, title=title, type="exam", subject=subject,
                                               due_at=datetime(2026, 11, day, 10, tzinfo=IST), confidence=0.9))
    add("CN lab viva", 24)
    add("Computer Networks midsem", 26, "Computer Networks")
    add("CD Quiz 1", 3, "CD")
    add("CD Quiz 2", 10, "CD")
    assert {e.status for e in repo.list_events(conn, a)} == {"upcoming"}
    add("CN midsem (rescheduled)", 28)
    statuses = {e.title: e.status for e in repo.list_events(conn, a)}
    assert statuses["Computer Networks midsem"] == statuses["CN midsem (rescheduled)"] == "conflict"
    assert statuses["CN lab viva"] == "upcoming"


@pytest.mark.parametrize("a,b,same", [
    ("DBMS", "Database Management Systems", True), ("OS", "Operating Systems", True),
    ("DBMS (CS301)", "CS301 Database Management Systems", True), ("OS", "DBMS", False),
    ("Operating Systems", "Computer Networks", False), (None, "OS", False),
])
def test_same_subject(a, b, same):
    assert repo.same_subject(a, b) is same


def test_multi_subject_schedule_rows_do_not_conflict(conn, users):
    a, _ = users
    s = _segment(conn, a)
    for day, title in ((21, "CS301 DBMS midsem"), (22, "CS302 OS midsem")):
        repo.add_event(conn, a, EventIn(segment_id=s, title=title, type="exam", subject="CS301, CS302",
                                        due_at=datetime(2026, 11, day, 10, tzinfo=IST), confidence=0.9))
    assert {e.status for e in repo.list_events(conn, a)} == {"upcoming"}


def test_all_day_and_timed_mentions_merge(conn, users):
    a, _ = users
    s = _segment(conn, a)
    e1 = repo.add_event(conn, a, EventIn(segment_id=s, title="OS exam", type="exam", subject="OS", all_day=True,
                                         due_at=datetime(2026, 11, 22, 9, tzinfo=IST), confidence=0.9))
    e2 = repo.add_event(conn, a, EventIn(segment_id=s, title="OS midsem", type="exam", subject="Operating Systems",
                                         due_at=datetime(2026, 11, 22, 10, tzinfo=IST), confidence=0.9))
    [ev] = repo.list_events(conn, a)
    assert e1 == e2 and ev.status == "upcoming" and not ev.all_day
    assert ev.due_at == "2026-11-22T04:30:00+00:00"  # the precise time wins


def test_course_code_subject_uses_title(conn, users):
    a, _ = users
    s = _segment(conn, a)
    assert repo.effective_subject("CS301", "CS301 Database Management Systems midsem") == "database management systems"
    assert repo.effective_subject("DBMS", "anything") == "DBMS"
    wa = repo.add_event(conn, a, EventIn(segment_id=s, title="DBMS midsem", type="exam", subject="DBMS",
                                         due_at=datetime(2026, 11, 21, 10, tzinfo=IST), confidence=0.9))
    pdf = repo.add_event(conn, a, EventIn(segment_id=s, title="CS301 Database Management Systems midsem", type="exam",
                                          subject="CS301", due_at=datetime(2026, 11, 21, 10, tzinfo=IST), confidence=0.9))
    assert wa == pdf  # same exam from the chat and the circular -> one event
    repo.add_event(conn, a, EventIn(segment_id=s, title="CS301 Database Management Systems midsem", type="exam",
                                    subject="CS301", due_at=datetime(2026, 11, 23, 14, tzinfo=IST), confidence=0.9))
    assert {e.status for e in repo.list_events(conn, a)} == {"conflict"}


# ---------- superseding (a newer "postponed" message replaces the old date) ----------

def _seg_at(conn, user_id, text, when):
    res = repo.ingest_source(conn, user_id, "whatsapp", repo.content_hash(text), [SegmentIn(text=text, observed_at=when)])
    return repo.list_segments(conn, user_id, res.source_id)[0].id


def _ev(seg_id, day, hour=10, title="DBMS midsem", subject="DBMS", typ="exam"):
    return EventIn(segment_id=seg_id, title=title, type=typ, subject=subject,
                   due_at=datetime(2026, 11, day, hour, tzinfo=IST), confidence=0.9)


def test_newer_update_supersedes_older(conn, users):
    a, _ = users
    old = repo.add_event(conn, a, _ev(_seg_at(conn, a, "DBMS midsem on 21 Nov", datetime(2026, 10, 1, tzinfo=IST)), 21))
    new = repo.add_event(conn, a, _ev(_seg_at(conn, a, "DBMS midsem postponed to 23 Nov, 2 pm",
                                              datetime(2026, 10, 14, tzinfo=IST)), 23, 14))
    assert repo.get_event(conn, a, old).status == "cancelled" and repo.get_event(conn, a, old).superseded_by == new
    assert [e.id for e in repo.list_events(conn, a)] == [new] and repo.get_event(conn, a, new).status == "upcoming"
    assert [e.id for e in repo.superseded_by_map(conn, a, [new])[new]] == [old]


def test_update_processed_before_original_still_wins(conn, users):
    a, _ = users  # e.g. a Gmail sync brings the old circular in after the WhatsApp update
    new = repo.add_event(conn, a, _ev(_seg_at(conn, a, "Deadline extended to 25 Nov", datetime(2026, 10, 14, tzinfo=IST)),
                                      25, 23, title="OS Assignment 3", subject="OS", typ="deadline"))
    old = repo.add_event(conn, a, _ev(_seg_at(conn, a, "OS Assignment 3 due 19 Nov", datetime(2026, 10, 1, tzinfo=IST)),
                                      19, 23, title="OS Assignment 3", subject="OS", typ="deadline"))
    assert repo.get_event(conn, a, old).superseded_by == new
    assert repo.get_event(conn, a, new).status == "upcoming"


def test_no_update_wording_stays_a_conflict(conn, users):
    a, _ = users
    e1 = repo.add_event(conn, a, _ev(_seg_at(conn, a, "DBMS midsem on 21 Nov", datetime(2026, 10, 1, tzinfo=IST)), 21))
    e2 = repo.add_event(conn, a, _ev(_seg_at(conn, a, "someone said DBMS midsem 23 Nov?", datetime(2026, 10, 2, tzinfo=IST)), 23))
    assert {repo.get_event(conn, a, e).status for e in (e1, e2)} == {"conflict"}


def test_undo_supersede_restores_conflict(conn, users):
    a, _ = users
    old = repo.add_event(conn, a, _ev(_seg_at(conn, a, "DBMS midsem on 21 Nov", datetime(2026, 10, 1, tzinfo=IST)), 21))
    new = repo.add_event(conn, a, _ev(_seg_at(conn, a, "DBMS midsem rescheduled to 23 Nov",
                                              datetime(2026, 10, 14, tzinfo=IST)), 23))
    assert repo.undo_supersede(conn, a, old)
    assert {repo.get_event(conn, a, e).status for e in (old, new)} == {"conflict"}
    assert not repo.undo_supersede(conn, a, old)


def test_resolve_existing_conflicts(conn, users):
    a, _ = users
    old_seg = _seg_at(conn, a, "DBMS midsem on 21 Nov", datetime(2026, 10, 1, tzinfo=IST))
    new_seg = _seg_at(conn, a, "IMPORTANT UPDATE: DBMS midsem postponed to 23 Nov", datetime(2026, 10, 14, tzinfo=IST))
    for seg_id, day in ((old_seg, 21), (new_seg, 23)):  # as stored before v5: both 'conflict'
        conn.execute("""INSERT INTO events (user_id, segment_id, title, type, subject, due_at, all_day, confidence,
                        status, valid_until) VALUES (?, ?, 'DBMS midsem', 'exam', 'DBMS', ?, 0, 0.9, 'conflict', ?)""",
                     (a, seg_id, repo.iso(datetime(2026, 11, day, 10, tzinfo=IST)),
                      repo.iso(datetime(2026, 11, day + 1, 10, tzinfo=IST))))
    assert repo.resolve_conflicts(conn, a) == 1
    statuses = {e.due_at[:10]: e.status for e in repo.list_events(conn, a, statuses=("upcoming", "conflict", "cancelled"))}
    assert statuses == {"2026-11-21": "cancelled", "2026-11-23": "upcoming"}


def test_same_author_time_change_supersedes(conn, users):
    """Reported bug: 'dbms class on saturday 3pm' then 'dbms class is on saturday 4pm' typed into the bot."""
    a, _ = users

    def typed(text, minutes):
        res = repo.ingest_source(conn, a, "telegram", repo.content_hash(text), [SegmentIn(
            text=text, observed_at=datetime(2026, 10, 17, 13, minutes, tzinfo=IST),
            locator={"chat_id": 1001, "message_id": minutes, "forward_sender": None})])
        return repo.list_segments(conn, a, res.source_id)[0].id
    first = repo.add_event(conn, a, EventIn(segment_id=typed("dbms class on saturday 3pm", 8), title="DBMS class",
                                            type="event", subject="DBMS", due_at=datetime(2026, 10, 17, 15, tzinfo=IST),
                                            confidence=0.9), now=datetime(2026, 10, 17, 7, tzinfo=IST))
    second = repo.add_event(conn, a, EventIn(segment_id=typed("dbms class is on saturday 4pm", 21), title="DBMS class",
                                             type="event", subject="DBMS", due_at=datetime(2026, 10, 17, 16, tzinfo=IST),
                                             confidence=0.9), now=datetime(2026, 10, 17, 7, tzinfo=IST))
    assert second != first  # a 1-hour change is a change, not a duplicate
    assert repo.get_event(conn, a, first).superseded_by == second
    assert repo.get_event(conn, a, second).status == "upcoming"


def test_different_authors_without_update_words_conflict(conn, users):
    a, _ = users

    def chat(text, sender, day):
        res = repo.ingest_source(conn, a, "whatsapp", repo.content_hash(text), [SegmentIn(
            text=text, observed_at=datetime(2026, 10, day, 10, tzinfo=IST),
            locator={"chat_name": "CSE", "sender": sender})])
        return repo.list_segments(conn, a, res.source_id)[0].id
    e1 = repo.add_event(conn, a, EventIn(segment_id=chat("OS quiz monday 10am", "Riya", 1), title="OS quiz", type="exam",
                                         subject="OS", due_at=datetime(2026, 11, 2, 10, tzinfo=IST), confidence=0.9))
    e2 = repo.add_event(conn, a, EventIn(segment_id=chat("OS quiz monday 11am i think", "Rohan", 2), title="OS quiz",
                                         type="exam", subject="OS", due_at=datetime(2026, 11, 2, 11, tzinfo=IST),
                                         confidence=0.9))
    assert {repo.get_event(conn, a, e).status for e in (e1, e2)} == {"conflict"}


def test_requeue_interrupted_jobs(conn, users):
    a, _ = users
    jid = repo.ingest_source(conn, a, "telegram", "h", [seg("x")]).job_id
    assert repo.claim_job(conn).id == jid  # processing, then the server is killed
    assert repo.requeue_interrupted_jobs(conn) == 1
    job = repo.get_job(conn, a, jid)
    assert job.status == "pending" and job.attempts == 0 and repo.claim_job(conn).id == jid
