"""User A must never be able to read, search, modify or delete user B's data."""
from datetime import datetime

import pytest

from app import repo
from app.models import ChunkIn, EventIn, SegmentIn
from tests.conftest import IST, seg, unit_vec


@pytest.fixture
def bob_data(conn, users):
    _, b = users
    text = "Bob's secret: Compiler Design viva on 25 Oct"
    res = repo.ingest_source(conn, b, "telegram", repo.content_hash(text), [seg(text)], title="bob note")
    seg_id = repo.list_segments(conn, b, res.source_id)[0].id
    chunk_id = repo.add_chunks(conn, b, seg_id, [ChunkIn(text=text, char_start=0, char_end=len(text))],
                               [unit_vec(7)])[0]
    ev = repo.add_event(conn, b, EventIn(segment_id=seg_id, title="CD viva", type="exam", subject="CD",
                                         due_at=datetime(2026, 10, 25, 10, tzinfo=IST), confidence=0.9))
    return {"source": res.source_id, "segment": seg_id, "chunk": chunk_id, "event": ev, "job": res.job_id}


def test_reads_are_scoped(conn, users, bob_data):
    a, b = users
    assert repo.get_source(conn, a, bob_data["source"]) is None
    assert repo.list_sources(conn, a) == []
    assert repo.list_segments(conn, a, bob_data["source"]) == []
    assert repo.get_segment(conn, a, bob_data["segment"]) is None
    assert repo.list_chunks(conn, a, bob_data["segment"]) == []
    assert repo.get_chunk_hits(conn, a, [bob_data["chunk"]]) == {}
    assert repo.list_events(conn, a) == []
    assert repo.get_job(conn, a, bob_data["job"]) is None
    # Sanity: Bob sees his own data.
    assert len(repo.list_sources(conn, b)) == 1 and len(repo.list_events(conn, b)) == 1


def test_search_is_scoped(conn, users, bob_data):
    a, b = users
    assert repo.fts_search(conn, a, '"compiler"') == []
    assert repo.vec_search(conn, a, unit_vec(7)) == []
    assert repo.fts_search(conn, b, '"compiler"')[0][0] == bob_data["chunk"]
    assert repo.vec_search(conn, b, unit_vec(7))[0][0] == bob_data["chunk"]


def test_writes_are_scoped(conn, users, bob_data):
    a, b = users
    assert not repo.delete_source(conn, a, bob_data["source"])
    assert not repo.set_event_status(conn, a, bob_data["event"], "cancelled")
    assert not repo.claim_reminder(conn, a, bob_data["event"], "24h")
    assert repo.mark_past_events(conn, a, now=datetime(2030, 1, 1, tzinfo=IST)) == 0
    with pytest.raises(PermissionError):
        repo.add_segments(conn, a, bob_data["source"],
                          [SegmentIn(text="x", observed_at=datetime(2026, 1, 1, tzinfo=IST))])
    with pytest.raises(PermissionError):
        repo.add_chunks(conn, a, bob_data["segment"], [ChunkIn(text="x", char_start=0, char_end=1)])
    with pytest.raises(PermissionError):
        repo.add_event(conn, a, EventIn(segment_id=bob_data["segment"], title="x", type="exam",
                                        due_at=datetime(2026, 11, 1, tzinfo=IST), confidence=0.9))
    # Bob's data is untouched.
    assert repo.get_source(conn, b, bob_data["source"]) is not None
    assert repo.list_events(conn, b)[0].status == "upcoming"
    assert len(repo.list_segments(conn, b, bob_data["source"])) == 1


def test_conflicts_do_not_cross_users(conn, users, bob_data):
    a, b = users
    res = repo.ingest_source(conn, a, "telegram", "ha", [seg("CD viva 27 Oct")])
    s = repo.list_segments(conn, a, res.source_id)[0].id
    repo.add_event(conn, a, EventIn(segment_id=s, title="CD viva", type="exam", subject="CD",
                                    due_at=datetime(2026, 10, 27, 10, tzinfo=IST), confidence=0.9))
    assert repo.list_events(conn, a)[0].status == "upcoming"
    assert repo.list_events(conn, b)[0].status == "upcoming"
