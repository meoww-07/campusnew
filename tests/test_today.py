import time as _time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import repo
from app.config import Settings
from app.db import init_db
from app.models import EventIn, SegmentIn
from app.organize import spaces
from app.retrieve import today
from app.retrieve.reminders import build_digest
from app.web import routes
from tests.fakes import FakeEmbedder

NOW = datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)  # Thu 22 Oct, 07:30 IST


def _item(conn, user_id, text, *, importance="medium", category="notice", subject=None, added=None,
          when=None, source="whatsapp"):
    res = repo.ingest_source(conn, user_id, source, repo.content_hash(text + str(user_id)),
                             [SegmentIn(text=text, observed_at=when or (added or NOW) - timedelta(hours=1))],
                             title=text[:30])
    conn.execute("UPDATE sources SET created_at = ? WHERE id = ?", (repo.iso(added or NOW - timedelta(hours=2)),
                                                                   res.source_id))
    seg = repo.list_segments(conn, user_id, res.source_id)[0].id
    repo.apply_extraction(conn, user_id, seg, summary=f"summary: {text}", category=category, subject=subject,
                          importance=importance, now=NOW)
    return seg


def _event(conn, user_id, title, due, *, typ="exam", subject=None, confidence=0.9, text=None):
    seg = _item(conn, user_id, text or f"{title} notice", added=NOW - timedelta(days=3))
    return repo.add_event(conn, user_id, EventIn(segment_id=seg, title=title, type=typ, subject=subject or title,
                                                 due_at=due, confidence=confidence), now=NOW - timedelta(days=3))


@pytest.fixture
def feed_data(conn, users):
    a, b = users
    ids = {
        "soon2": _event(conn, a, "OS Assignment 3", NOW + timedelta(hours=40), typ="deadline", subject="OS"),
        "soon1": _event(conn, a, "CD Quiz 2", NOW + timedelta(hours=5), subject="CD"),
        "week": _event(conn, a, "CN lab viva", NOW + timedelta(days=4), subject="CN"),
        "far": _event(conn, a, "SE review", NOW + timedelta(days=12), subject="SE"),
        "unsure": _event(conn, a, "Hackathon registration", NOW + timedelta(days=3), typ="deadline",
                         subject="Hackathon", confidence=0.6),
        "c1": _event(conn, a, "DBMS midsem", NOW + timedelta(days=1), subject="DBMS", text="DBMS midsem 23rd"),
        "c2": _event(conn, a, "DBMS midsem", NOW + timedelta(days=2), subject="DBMS", text="DBMS midsem 24th?"),
        "new_high": _item(conn, a, "Fee deadline circular", importance="high", category="deadline"),
        "new_med": _item(conn, a, "Library timings changed", importance="medium"),
        "new_low": _item(conn, a, "ok thanks", importance="low"),
        "old_res": _item(conn, a, "DBMS PYQ solutions pdf", importance="high", category="resource",
                         added=NOW - timedelta(days=20)),
    }
    _event(conn, b, "Bob exam", NOW + timedelta(hours=3))
    return a, b, ids


def section(feed, key):
    return next((s for s in feed["sections"] if s["key"] == key), None)


def test_today_sections_and_ranking(conn, feed_data):
    a, _, ids = feed_data
    feed = today.build_today(conn, repo.get_user(conn, a), NOW, since=NOW - timedelta(days=1))
    due = section(feed, "due_soon")["items"]
    assert [e["title"] for e in due] == ["CD Quiz 2", "DBMS midsem", "OS Assignment 3", "DBMS midsem"]
    assert due[0]["seconds_until"] == 5 * 3600 and due[0]["reminders_sent"] == []
    att = section(feed, "attention")["items"]
    assert [a_["kind"] for a_ in att] == ["conflict", "low_confidence"]
    assert {e["id"] for e in att[0]["events"]} == {ids["c1"], ids["c2"]}
    assert att[1]["events"][0]["id"] == ids["unsure"]
    new = section(feed, "new")["items"]
    assert [s["segment_id"] for s in new[:2]] == [ids["new_high"], ids["new_med"]]  # high first
    assert ids["new_low"] not in {s["segment_id"] for s in new}
    week = section(feed, "week")["items"]
    assert [(d["label"], [e["title"] for e in d["events"]]) for d in week] == [
        ("Sun 25 Oct", ["Hackathon registration"]), ("Mon 26 Oct", ["CN lab viva"])]
    assert all(e["title"] != "SE review" for d in week for e in d["events"])
    assert [s["segment_id"] for s in section(feed, "rediscover")["items"]] == [ids["old_res"]]
    assert section(feed, "spaces") is None  # nothing pinned -> section omitted
    assert feed["summary"] == ("3 exams and 1 deadline in the next 48 hours, 1 conflict to check, "
                               "1 date to confirm and 2 new items.")
    assert feed["greeting"].startswith("Good morning")
    assert "Bob exam" not in str(feed)


def test_new_since_last_seen_and_mark_seen(conn, feed_data):
    a, _, ids = feed_data
    user = repo.get_user(conn, a)
    assert section(today.build_today(conn, user, NOW), "new") is not None  # never looked: last 24h
    repo.set_last_seen_today(conn, a, NOW)
    assert section(today.build_today(conn, repo.get_user(conn, a), NOW), "new") is None
    later = _item(conn, a, "Placement drive on Monday", importance="high", added=NOW + timedelta(minutes=5))
    feed = today.build_today(conn, repo.get_user(conn, a), NOW + timedelta(minutes=10))
    assert [s["segment_id"] for s in section(feed, "new")["items"]] == [later]


def test_snooze_hides_until_tomorrow(conn, feed_data):
    a, _, ids = feed_data
    user = repo.get_user(conn, a)
    until = today.snooze_until(user, NOW)
    assert until.isoformat() == "2026-10-23T07:00:00+05:30"
    repo.snooze(conn, a, "event", ids["soon1"], until)
    repo.snooze(conn, a, "segment", ids["new_high"], until)
    feed = today.build_today(conn, user, NOW, since=NOW - timedelta(days=1))
    assert "CD Quiz 2" not in [e["title"] for e in section(feed, "due_soon")["items"]]
    assert ids["new_high"] not in [s["segment_id"] for s in section(feed, "new")["items"]]
    back = today.build_today(conn, user, until + timedelta(minutes=1), since=NOW - timedelta(days=1))
    assert ids["new_high"] in [s["segment_id"] for s in section(back, "new")["items"]]
    with pytest.raises(ValueError):
        repo.snooze(conn, a, "space", 1, until)


def test_rediscover_excludes_opened(conn, feed_data):
    a, _, ids = feed_data
    repo.mark_segment_opened(conn, a, ids["old_res"], NOW)
    assert section(today.build_today(conn, repo.get_user(conn, a), NOW), "rediscover") is None


def test_pinned_space_section(conn, feed_data):
    a, _, _ = feed_data
    for i in range(3):
        _item(conn, a, f"OS notes part {i}", subject="OS", when=NOW - timedelta(hours=10 - i))
    spaces.backfill_user(conn, a)
    os_space = next(s for s in repo.list_spaces(conn, a) if s.name == "OS")
    repo.update_space(conn, a, os_space.id, pinned=True)
    sec = section(today.build_today(conn, repo.get_user(conn, a), NOW), "spaces")
    assert sec["items"][0]["space"]["name"] == "OS" and len(sec["items"][0]["items"]) == 2


def test_failed_jobs_need_attention(conn, users):
    a, _ = users
    res = repo.ingest_source(conn, a, "upload", "bad", [], title="blurry.png", job_kind="image")
    for _ in range(3):
        repo.claim_job(conn)
        repo.fail_job(conn, res.job_id, "OcrUnavailable: no text")
    att = section(today.build_today(conn, repo.get_user(conn, a), NOW), "attention")["items"]
    assert att[0]["kind"] == "failed_job" and att[0]["title"] == "blurry.png"


def test_empty_feed(conn, users):
    _, b = users
    feed = today.build_today(conn, repo.get_user(conn, b), NOW)
    assert feed["sections"] == [] and feed["summary"] == "You're all caught up."


def test_digest_uses_today_feed(conn, feed_data):
    a, _, _ = feed_data
    text = build_digest(conn, repo.get_user(conn, a), NOW)
    feed = today.build_today(conn, repo.get_user(conn, a), NOW, since=NOW - timedelta(hours=24),
                             new_importances=("high",))
    assert feed["summary"] in text  # same counts as the website
    assert "Today:\n• CD Quiz 2" in text and "Tomorrow:" in text
    assert "DBMS midsem: sources disagree" in text and "Fee deadline circular" in text
    assert "Library timings" not in text  # digest: high importance only


@pytest.mark.parametrize("seconds,label", [(-5, "overdue"), (300, "in 5m"), (3 * 3600, "in 3h"),
                                           (3 * 3600 + 1200, "in 3h 20m"), (2 * 86400 + 5, "in 2 days")])
def test_seconds_label(seconds, label):
    assert today.seconds_label(seconds) == label


# ---------- API, isolation, performance ----------

@pytest.fixture
def api(tmp_path):
    from app import main
    settings = Settings(db_path=tmp_path / "web.db", files_dir=tmp_path / "files", session_secret="t",
                        app_base_url="http://testserver")
    conn = init_db(settings.db_path)
    main.app.dependency_overrides[routes.settings_dep] = lambda: settings
    main.app.state.embedder, main.app.state.llm, main.app.state.telegram = FakeEmbedder(), None, None
    a, b = repo.upsert_user(conn, 1, "Asha Rao").id, repo.upsert_user(conn, 2, "Bob").id

    def client(user_id):
        c = TestClient(main.app, headers={"X-CampusMind": "1"})
        c.get(f"/login?token={repo.create_login_token(conn, user_id)}", follow_redirects=False)
        return c
    yield conn, a, b, client(a), client(b)
    main.app.dependency_overrides.clear()
    conn.close()


def test_today_api_mark_seen_snooze_open(api):
    conn, a, b, ca, cb = api
    now = datetime.now(timezone.utc)
    seg = _item(conn, a, "Placement drive by TCS", importance="high", added=now - timedelta(minutes=5))
    ev = repo.create_event(conn, a, title="TCS test", type="exam", due_at=now + timedelta(hours=6))
    feed = ca.get("/api/today?mark_seen=true").json()
    assert feed["greeting"].endswith("Asha") and section(feed, "new") and section(feed, "due_soon")
    assert section(ca.get("/api/today").json(), "new") is None  # seen
    assert ca.post("/api/today/snooze", json={"kind": "event", "id": ev.id}).status_code == 200
    assert section(ca.get("/api/today").json(), "due_soon") is None
    detail = ca.get(f"/api/segments/{seg}").json()
    assert detail["text"] == "Placement drive by TCS" and detail["opened_at"] is None and "WhatsApp" in detail["label"]
    assert ca.post(f"/api/segments/{seg}/opened").json() == {"opened": True}
    assert ca.get(f"/api/segments/{seg}").json()["opened_at"]
    # isolation
    assert cb.get(f"/api/segments/{seg}").status_code == 404
    assert cb.post(f"/api/segments/{seg}/opened").status_code == 404
    assert cb.post("/api/today/snooze", json={"kind": "segment", "id": seg}).status_code == 404
    assert "TCS" not in str(cb.get("/api/today").json())


def test_today_and_calendar_fast_on_seed(api, tmp_path):
    """Spec: Today and calendar responses under 300 ms on the seed dataset."""
    from app.seed import load_demo
    conn, a, _, ca, _ = api
    counts = load_demo(conn, Settings(files_dir=tmp_path / "f"), repo.get_user(conn, a), FakeEmbedder())
    assert counts["segments"] >= 14 and counts["events"] >= 12
    ca.get("/api/today")  # warm up
    for url in ("/api/today", "/api/events?days=31", "/api/spaces"):
        t0 = _time.perf_counter()
        r = ca.get(url)
        ms = (_time.perf_counter() - t0) * 1000
        assert r.status_code == 200 and ms < 300, (url, round(ms))


def test_demo_seed_looks_right(conn, users, tmp_path):
    from app.seed import load_demo
    a, _ = users
    now = datetime.now(timezone.utc)
    load_demo(conn, Settings(files_dir=tmp_path), repo.get_user(conn, a), FakeEmbedder(), now)
    assert load_demo(conn, Settings(files_dir=tmp_path), repo.get_user(conn, a), FakeEmbedder(), now)["segments"] == 0
    names = {s.name: s for s in repo.list_spaces(conn, a)}
    assert {"DBMS", "OS", "Compiler Design", "SE", "Placement Cell", "Placements"} <= set(names)
    assert names["DBMS"].pinned and names["Placement Cell"].pinned and names["Placement Cell"].kind == "custom"
    assert len(repo.space_segment_ids(conn, a, names["Placement Cell"].id)) == 2  # tpo@ mails only
    events = repo.list_events(conn, a, start=now, end=now + timedelta(days=31))
    assert len(events) >= 10
    assert any(e.status == "conflict" for e in events)  # CD quiz 3: two people, two dates
    assert any(now <= datetime.fromisoformat(e.due_at) <= now + timedelta(hours=24) for e in events)
    se = [e for e in repo.list_events(conn, a, statuses=("upcoming", "cancelled")) if e.title == "SE project review"]
    assert sorted(e.status for e in se) == ["cancelled", "upcoming"]  # the email update superseded the old date
    feed = today.build_today(conn, repo.get_user(conn, a), now)
    keys = [s["key"] for s in feed["sections"]]
    assert keys[:2] == ["due_soon", "attention"] and "spaces" in keys and "rediscover" in keys
