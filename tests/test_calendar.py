import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import repo
from app.config import Settings
from app.db import init_db
from app.models import EventIn, SegmentIn
from app.retrieve import reminders as rem
from app.web import routes
from app.web.calendar_ics import build_ics
from tests.conftest import IST
from tests.fakes import FakeEmbedder

NOW = datetime.now(timezone.utc)


def _seg(conn, user_id, text="notice", when=None):
    res = repo.ingest_source(conn, user_id, "telegram", repo.content_hash(text + str(user_id)),
                             [SegmentIn(text=text, observed_at=when or NOW - timedelta(days=3))])
    return repo.list_segments(conn, user_id, res.source_id)[0].id


def _event(conn, user_id, due, title="OS midsem", subject="OS", typ="exam", text=None):
    seg = _seg(conn, user_id, text or f"{title} {due.isoformat()}")
    return repo.add_event(conn, user_id, EventIn(segment_id=seg, title=title, type=typ, subject=subject,
                                                 due_at=due, confidence=0.9), now=NOW - timedelta(days=30))


# ---------- range queries across timezones ----------

def test_range_uses_utc_instants_not_dates(conn, users):
    a, _ = users
    just_after_midnight_ist = datetime(2026, 11, 21, 0, 30, tzinfo=IST)  # = 20 Nov 19:00 UTC
    e = _event(conn, a, just_after_midnight_ist)
    ist_day = (datetime(2026, 11, 21, tzinfo=IST), datetime(2026, 11, 21, 23, 59, 59, tzinfo=IST))
    utc_day = (datetime(2026, 11, 21, tzinfo=timezone.utc), datetime(2026, 11, 21, 23, 59, 59, tzinfo=timezone.utc))
    assert [x.id for x in repo.list_events_range(conn, a, *ist_day)] == [e]
    assert repo.list_events_range(conn, a, *utc_day) == []  # it's still 20 Nov in UTC
    ny = ZoneInfo("America/New_York")  # same instant seen from another timezone
    assert [x.id for x in repo.list_events_range(conn, a, datetime(2026, 11, 20, 13, tzinfo=ny),
                                                 datetime(2026, 11, 20, 15, tzinfo=ny))] == [e]


def test_range_includes_multi_day_and_filters(conn, users):
    a, _ = users
    fest = repo.create_event(conn, a, title="Tech fest", type="event", due_at=datetime(2026, 11, 5, 9, tzinfo=IST),
                             end_at=datetime(2026, 11, 7, 18, tzinfo=IST))
    exam = _event(conn, a, datetime(2026, 11, 6, 10, tzinfo=IST))
    inside = (datetime(2026, 11, 6, tzinfo=IST), datetime(2026, 11, 6, 23, tzinfo=IST))
    assert {x.id for x in repo.list_events_range(conn, a, *inside)} == {fest.id, exam}
    assert [x.id for x in repo.list_events_range(conn, a, *inside, types=["exam"])] == [exam]


def test_range_by_space(conn, users):
    from app.organize import spaces
    a, _ = users
    e1 = _event(conn, a, datetime(2026, 11, 10, 10, tzinfo=IST), title="CN viva", subject="CN")
    _event(conn, a, datetime(2026, 11, 11, 10, tzinfo=IST), title="CN midsem", subject="CN")
    e3 = _event(conn, a, datetime(2026, 11, 12, 10, tzinfo=IST), title="OS quiz", subject="OS")
    spaces.backfill_user(conn, a)
    cn = next(s for s in repo.list_spaces(conn, a) if s.name == "CN")
    got = {x.id for x in repo.list_events_range(conn, a, datetime(2026, 11, 1, tzinfo=IST),
                                                datetime(2026, 11, 30, tzinfo=IST), space_id=cn.id)}
    assert e1 in got and e3 not in got and len(got) == 2


# ---------- edits ----------

def test_user_edited_events_are_not_overwritten(conn, users):
    a, _ = users
    seg = _seg(conn, a, "OS midsem 22 Nov")
    e = repo.add_event(conn, a, EventIn(segment_id=seg, title="OS midsem", type="exam", subject="OS", all_day=True,
                                        due_at=datetime(2026, 11, 22, 9, tzinfo=IST), confidence=0.6))
    repo.update_event(conn, a, e, title="OS midsem (Hall B)", location="Hall B")
    # Re-extraction of a later mention with a precise time must not touch the edited event.
    seg2 = _seg(conn, a, "OS midsem 22 Nov 10am")
    same = repo.add_event(conn, a, EventIn(segment_id=seg2, title="OS midsem", type="exam", subject="OS",
                                           due_at=datetime(2026, 11, 22, 10, tzinfo=IST), confidence=0.9))
    ev = repo.get_event(conn, a, e)
    assert same == e and ev.all_day and ev.title == "OS midsem (Hall B)" and ev.user_edited
    # A newer "postponed" message can't silently retire it either: flagged instead.
    seg3 = _seg(conn, a, "OS midsem postponed to 25 Nov", when=NOW)
    repo.add_event(conn, a, EventIn(segment_id=seg3, title="OS midsem", type="exam", subject="OS",
                                    due_at=datetime(2026, 11, 25, 10, tzinfo=IST), confidence=0.9))
    assert repo.get_event(conn, a, e).status == "conflict"


def test_date_change_bumps_version_and_resets_reminders(conn, users):
    a, _ = users
    e = _event(conn, a, NOW + timedelta(hours=10))
    sent = []

    async def send(chat, text):
        sent.append(text)
    asyncio.run(rem.send_due_reminders(conn, send, NOW))
    asyncio.run(rem.send_due_reminders(conn, send, NOW + timedelta(minutes=5)))
    assert len(sent) == 1
    repo.update_event(conn, a, e, title="OS midsem (renamed)")  # no date change: no new reminder
    asyncio.run(rem.send_due_reminders(conn, send, NOW + timedelta(minutes=10)))
    assert len(sent) == 1 and repo.get_event(conn, a, e).version == 1
    repo.update_event(conn, a, e, due_at=NOW + timedelta(hours=12))
    assert repo.get_event(conn, a, e).version == 2
    asyncio.run(rem.send_due_reminders(conn, send, NOW + timedelta(minutes=15)))
    assert len(sent) == 2 and "renamed" in sent[1]


def test_update_validation_and_status(conn, users):
    a, _ = users
    e = _event(conn, a, NOW + timedelta(days=2))
    with pytest.raises(ValueError):
        repo.update_event(conn, a, e, status="conflict")
    with pytest.raises(ValueError):
        repo.update_event(conn, a, e, end_at=NOW)  # before start
    with pytest.raises(ValueError):
        repo.update_event(conn, a, e, superseded_by=99)
    assert repo.update_event(conn, a, e, status="done").status == "done"
    assert repo.list_events(conn, a) == []  # done events leave the upcoming list
    assert repo.mark_past_events(conn, a, NOW + timedelta(days=5)) == 0  # done stays done


def test_resolve_conflict_actions(conn, users):
    a, _ = users
    old = _event(conn, a, datetime(2026, 11, 21, 10, tzinfo=IST), title="DBMS midsem", subject="DBMS",
                 text="DBMS midsem on 21 Nov")
    new = _event(conn, a, datetime(2026, 11, 23, 14, tzinfo=IST), title="DBMS midsem", subject="DBMS",
                 text="someone said DBMS midsem 23 Nov")
    assert {repo.get_event(conn, a, x).status for x in (old, new)} == {"conflict"}
    assert [p.id for p in repo.conflict_partners(conn, a, new)] == [old]
    repo.resolve_conflict(conn, a, new, "update")
    assert repo.get_event(conn, a, old).superseded_by == new and repo.get_event(conn, a, new).status == "upcoming"
    assert repo.undo_supersede(conn, a, old)
    repo.resolve_conflict(conn, a, old, "keep_both")
    assert {repo.get_event(conn, a, x).status for x in (old, new)} == {"upcoming"}
    with pytest.raises(ValueError):
        repo.resolve_conflict(conn, a, old, "whatever")


# ---------- iCal ----------

def test_ics_feed_format(conn, users):
    a, _ = users
    timed = repo.create_event(conn, a, title="DBMS midsem, LH-3; bring ID", type="exam",
                              due_at=datetime(2026, 11, 23, 14, tzinfo=IST), location="LH-3")
    allday = repo.create_event(conn, a, title="Fee deadline", type="deadline", all_day=True,
                               due_at=datetime(2026, 11, 30, 23, 59, tzinfo=IST))
    cancelled = repo.create_event(conn, a, title="Old date", type="exam", due_at=datetime(2026, 11, 21, 10, tzinfo=IST))
    repo.update_event(conn, a, cancelled.id, status="cancelled")
    events = [repo.get_event(conn, a, x.id) for x in (timed, allday, cancelled)]
    ics = build_ics(events, "Asia/Kolkata", "CampusMind", "https://cm.test", NOW)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "DTSTART:20261123T083000Z" in ics and "DTEND:20261123T093000Z" in ics  # 14:00 IST = 08:30 UTC
    assert "SUMMARY:DBMS midsem\\, LH-3\\; bring ID" in ics and "LOCATION:LH-3" in ics
    assert "DTSTART;VALUE=DATE:20261130" in ics and "DTEND;VALUE=DATE:20261201" in ics
    assert "Old date" not in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


# ---------- API + isolation ----------

@pytest.fixture
def api(tmp_path):
    from app import main
    settings = Settings(db_path=tmp_path / "web.db", files_dir=tmp_path / "files", session_secret="t",
                        app_base_url="http://testserver")
    conn = init_db(settings.db_path)
    main.app.dependency_overrides[routes.settings_dep] = lambda: settings
    main.app.state.embedder, main.app.state.llm, main.app.state.telegram = FakeEmbedder(), None, None
    a, b = repo.upsert_user(conn, 1, "A").id, repo.upsert_user(conn, 2, "B").id

    def client(user_id):
        c = TestClient(main.app, headers={"X-CampusMind": "1"})
        c.get(f"/login?token={repo.create_login_token(conn, user_id)}", follow_redirects=False)
        return c
    yield conn, a, b, client(a), client(b)
    main.app.dependency_overrides.clear()
    conn.close()


def test_events_api_crud_and_range(api):
    conn, a, _, ca, _ = api
    r = ca.post("/api/events", json={"title": "Project demo", "type": "event", "start": "2026-11-12T15:00:00",
                                     "end": "2026-11-12T16:00:00", "location": "Lab 2"})
    assert r.status_code == 201
    ev = r.json()
    assert ev["due_at"] == "2026-11-12T09:30:00+00:00"  # naive = user's timezone (IST)
    assert ev["manual"] and ev["user_edited"] and ev["color"] == "#3d5afe"
    allday = ca.post("/api/events", json={"title": "Fees", "type": "deadline", "start": "2026-11-30",
                                          "all_day": True}).json()
    assert allday["due_at"] == "2026-11-30T18:29:00+00:00"  # 23:59 IST
    got = ca.get("/api/events?start=2026-11-01T00:00:00%2B05:30&end=2026-11-30T23:59:59%2B05:30").json()
    assert [e["title"] for e in got] == ["Project demo", "Fees"]
    assert [e["title"] for e in ca.get("/api/events?start=2026-11-01&end=2026-11-30T23:59&type=deadline").json()] == ["Fees"]
    p = ca.patch(f"/api/events/{ev['id']}", json={"start": "2026-11-13T15:00:00", "end": "2026-11-13T16:00:00"}).json()
    assert p["version"] == 2 and p["due_at"].startswith("2026-11-13")
    assert ca.patch(f"/api/events/{ev['id']}", json={"status": "done"}).json()["status"] == "done"
    assert ca.patch(f"/api/events/{ev['id']}", json={"status": "past"}).status_code == 422
    assert ca.get(f"/api/events/{ev['id']}").json()["conflicts_with"] == []
    assert ca.delete(f"/api/events/{ev['id']}").json() == {"deleted": True}
    assert ca.get(f"/api/events/{ev['id']}").status_code == 404
    assert ca.get("/api/events?start=2026-01-01&end=2027-12-31").status_code == 400  # too large


def test_conflict_resolution_api(api):
    conn, a, _, ca, _ = api
    old = _event(conn, a, datetime(2026, 11, 21, 10, tzinfo=IST), title="DBMS midsem", subject="DBMS",
                 text="DBMS midsem on 21 Nov")
    new = _event(conn, a, datetime(2026, 11, 23, 14, tzinfo=IST), title="DBMS midsem", subject="DBMS",
                 text="DBMS midsem 23 Nov maybe")
    detail = ca.get(f"/api/events/{new}").json()
    assert detail["status"] == "conflict" and [c["id"] for c in detail["conflicts_with"]] == [old]
    out = ca.post(f"/api/events/{new}/resolve", json={"action": "update"}).json()
    assert out[0]["status"] == "upcoming" and out[1]["status"] == "cancelled"
    assert ca.get(f"/api/events/{new}").json()["replaces"][0]["id"] == old
    assert ca.post(f"/api/events/{old}/restore").json()["status"] == "conflict"
    assert ca.post(f"/api/events/{new}/restore").status_code == 400


def test_ics_subscription_api(api):
    conn, a, _, ca, _ = api
    repo.create_event(conn, a, title="CN viva", type="exam", due_at=NOW + timedelta(days=3))
    sub = ca.get("/api/calendar/subscription").json()
    assert sub["url"].startswith("http://testserver/api/calendar.ics?token=")
    assert sub["webcal"].startswith("webcal://testserver/")
    token = sub["url"].split("token=")[1]
    anon = TestClient(ca.app)
    r = anon.get(f"/api/calendar.ics?token={token}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar") and "CN viva" in r.text
    new = ca.post("/api/calendar/subscription/regenerate").json()["url"]
    assert new != sub["url"]
    assert anon.get(f"/api/calendar.ics?token={token}").status_code == 404  # old URL revoked
    assert anon.get("/api/calendar.ics?token=nope-nope-nope").status_code == 404


def test_calendar_api_isolation(api):
    conn, a, b, ca, cb = api
    e = repo.create_event(conn, a, title="Alice exam", type="exam", due_at=NOW + timedelta(days=2))
    assert cb.get("/api/events").json() == []
    for method, url, body in [("get", f"/api/events/{e.id}", None), ("patch", f"/api/events/{e.id}", {"title": "x"}),
                              ("delete", f"/api/events/{e.id}", None),
                              ("post", f"/api/events/{e.id}/resolve", {"action": "update"}),
                              ("post", f"/api/events/{e.id}/restore", None)]:
        r = getattr(cb, method)(url, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (method, url, r.status_code)
    assert repo.get_event(conn, a, e.id).title == "Alice exam"
    token_b = cb.get("/api/calendar/subscription").json()["url"].split("token=")[1]
    assert "Alice exam" not in TestClient(ca.app).get(f"/api/calendar.ics?token={token_b}").text
