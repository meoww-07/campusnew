import asyncio
from datetime import datetime, timedelta, timezone

from app import repo
from app.models import EventIn, SegmentIn
from app.retrieve import reminders as rem
from tests.conftest import IST

NOW = datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)  # 07:30 IST, Thu 22 Oct


class Outbox:
    def __init__(self, fail: int = 0) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def __call__(self, chat_id: int, text: str) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text))


def _event(conn, user_id, title, due, subject=None, typ="exam", text="notice"):
    res = repo.ingest_source(conn, user_id, "telegram", repo.content_hash(text + title),
                             [SegmentIn(text=text, observed_at=NOW - timedelta(days=5))])
    seg = repo.list_segments(conn, user_id, res.source_id)[0].id
    return repo.add_event(conn, user_id, EventIn(segment_id=seg, title=title, type=typ, subject=subject or title,
                                                 due_at=due, confidence=0.9), now=NOW - timedelta(days=5))


def test_reminder_kind():
    from app.models import Event
    def ev(hours):
        due = repo.iso(NOW + timedelta(hours=hours))
        return Event(id=1, user_id=1, segment_id=1, title="t", type="exam", due_at=due, confidence=1,
                     valid_until=due)
    assert rem.reminder_kind(ev(30), NOW) is None
    assert rem.reminder_kind(ev(20), NOW) == "24h"
    assert rem.reminder_kind(ev(1.5), NOW) == "2h"
    assert rem.reminder_kind(ev(-1), NOW) is None


def test_24h_then_2h_each_sent_once(conn, users):
    a, b = users
    _event(conn, a, "DBMS midsem", NOW + timedelta(hours=20))
    _event(conn, b, "Bob exam", NOW + timedelta(days=3))
    out = Outbox()
    assert asyncio.run(rem.send_due_reminders(conn, out, NOW)) == 1
    assert out.sent[0][0] == 1001 and "DBMS midsem" in out.sent[0][1] and "within 24h" in out.sent[0][1]
    assert asyncio.run(rem.send_due_reminders(conn, out, NOW + timedelta(minutes=5))) == 0  # no duplicate
    assert asyncio.run(rem.send_due_reminders(conn, out, NOW + timedelta(hours=18, minutes=30))) == 1
    assert "2 hours" in out.sent[1][1]
    assert asyncio.run(rem.send_due_reminders(conn, out, NOW + timedelta(hours=19))) == 0


def test_late_event_gets_only_2h(conn, users):
    a, _ = users
    _event(conn, a, "Surprise quiz", NOW + timedelta(hours=1))
    out = Outbox()
    asyncio.run(rem.send_due_reminders(conn, out, NOW))
    asyncio.run(rem.send_due_reminders(conn, out, NOW + timedelta(minutes=5)))
    assert len(out.sent) == 1 and "2 hours" in out.sent[0][1]


def test_failed_send_is_retried(conn, users):
    a, _ = users
    _event(conn, a, "OS assignment", NOW + timedelta(hours=10), typ="deadline")
    out = Outbox(fail=1)
    assert asyncio.run(rem.send_due_reminders(conn, out, NOW)) == 0
    assert asyncio.run(rem.send_due_reminders(conn, out, NOW + timedelta(minutes=5))) == 1


def test_conflict_reminder_mentions_conflict(conn, users):
    a, _ = users
    _event(conn, a, "DBMS midsem", NOW + timedelta(hours=10), subject="DBMS", text="old")
    _event(conn, a, "DBMS midsem", NOW + timedelta(hours=40), subject="DBMS", text="new")
    out = Outbox()
    asyncio.run(rem.send_due_reminders(conn, out, NOW))
    assert "disagree" in out.sent[0][1]


def test_digest_today_tomorrow_and_important(conn, users):
    a, b = users
    _event(conn, a, "OS midsem", datetime(2026, 10, 22, 10, tzinfo=IST))
    _event(conn, a, "DBMS midsem", datetime(2026, 10, 23, 14, tzinfo=IST))
    _event(conn, a, "CN midsem", datetime(2026, 10, 26, 10, tzinfo=IST))
    res = repo.ingest_source(conn, a, "upload", "fee", [SegmentIn(text="Fee circular", observed_at=NOW)])
    conn.execute("UPDATE sources SET created_at = ? WHERE id = ?", (repo.iso(NOW - timedelta(hours=3)), res.source_id))
    seg = repo.list_segments(conn, a, res.source_id)[0].id
    repo.apply_extraction(conn, a, seg, summary="Tuition fee due 31 Oct", category="deadline",
                          subject=None, importance="high")
    user = repo.get_user(conn, a)
    text = rem.build_digest(conn, user, now=NOW)
    assert "Today:\n• OS midsem" in text and "Tomorrow:\n• DBMS midsem" in text
    assert "CN midsem" not in text and "Tuition fee due 31 Oct" in text
    assert rem.build_digest(conn, repo.get_user(conn, b), NOW) is None  # empty -> skipped
    out = Outbox()
    asyncio.run(rem.send_digests(conn, out, NOW))
    assert [c for c, _ in out.sent] == [1001]


def test_mark_past_job(conn, users):
    a, _ = users
    _event(conn, a, "Old quiz", NOW - timedelta(days=2))
    assert asyncio.run(rem.mark_past(conn, NOW)) == 1
    assert repo.list_events(conn, a) == []


def test_scheduler_jobs():
    async def build():
        s = rem.build_scheduler(None, Outbox())  # type: ignore[arg-type]
        return {j.id: str(j.trigger) for j in s.get_jobs()}
    jobs = asyncio.run(build())
    assert set(jobs) == {"reminders", "digest", "lifecycle"}
    assert "hour='7', minute='30'" in jobs["digest"] and "0:05:00" in jobs["reminders"]


def test_reminder_and_upcoming_mention_moved_date(conn, users):
    from app.ingest.telegram_bot import format_events
    from app.retrieve.answer import moved_from
    a, _ = users
    res1 = repo.ingest_source(conn, a, "whatsapp", "o", [SegmentIn(text="DBMS midsem on Tue", observed_at=NOW - timedelta(days=9))])
    res2 = repo.ingest_source(conn, a, "whatsapp", "n", [SegmentIn(text="DBMS midsem postponed", observed_at=NOW - timedelta(days=2))])
    old = repo.add_event(conn, a, EventIn(segment_id=repo.list_segments(conn, a, res1.source_id)[0].id, title="DBMS midsem",
                                          type="exam", subject="DBMS", due_at=NOW + timedelta(hours=10), confidence=0.9), now=NOW)
    new = repo.add_event(conn, a, EventIn(segment_id=repo.list_segments(conn, a, res2.source_id)[0].id, title="DBMS midsem",
                                          type="exam", subject="DBMS", due_at=NOW + timedelta(hours=20), confidence=0.9), now=NOW)
    out = Outbox()
    asyncio.run(rem.send_due_reminders(conn, out, NOW))
    assert len(out.sent) == 1 and "Date changed: moved from" in out.sent[0][1]  # old date gets no reminder
    events = repo.list_events(conn, a)
    assert [e.id for e in events] == [new]
    assert "🔁 moved from" in format_events(events, "Asia/Kolkata", moved_from(conn, a, events, "Asia/Kolkata"))
