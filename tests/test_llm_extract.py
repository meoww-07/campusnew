import json
from datetime import datetime

import pytest

from app import repo
from app.llm import LLMError, chat_json, parse_json_model
from app.models import ExtractedEvent, Extraction, SegmentIn
from app.organize import llm_extract, worker
from tests.conftest import IST
from tests.fakes import FakeEmbedder, FakeLLM

OBS = datetime(2026, 10, 16, 10, 20, tzinfo=IST)  # a Friday


def ev(**kw) -> ExtractedEvent:
    base = dict(title="CD Quiz 2", type="exam", date_text="next Tuesday 9 am",
                due_at=datetime(2026, 10, 20, 9, tzinfo=IST), all_day=False, confidence=0.9)
    return ExtractedEvent(**{**base, **kw})


def payload(events=(), **kw) -> str:
    d = {"summary": "s", "category": "exam", "subject": "CD", "importance": "high", "events": list(events)}
    d.update(kw)
    return json.dumps(d)


# ---------- JSON parsing / retry ----------

def test_parse_tolerates_fences():
    raw = "Sure!\n```json\n" + payload() + "\n```"
    assert parse_json_model(raw, Extraction).subject == "CD"


def test_retry_once_then_fail():
    llm = FakeLLM(["not json", payload()])
    assert chat_json(llm, "sys", "user", Extraction).category == "exam"
    assert "previous reply was invalid" in llm.prompts[1][1]
    with pytest.raises(LLMError):
        chat_json(FakeLLM(["nope", "still not json"]), "sys", "user", Extraction)


def test_lenient_extraction_drops_only_bad_events():
    raw = payload([
        {"title": "Fee notice", "type": "notice", "due_at": "2026-10-31T23:59:00+05:30", "confidence": 0.9},
        {"title": "Broken", "type": "exam", "due_at": "sometime soon", "confidence": 0.9},
        {"title": "OS quiz", "type": "Exam", "due_at": "2026-10-20T09:00:00+05:30", "confidence": 0.8},
    ], category="announcement", importance="URGENT")
    ex = parse_json_model(raw, Extraction)
    assert [(e.title, e.type) for e in ex.events] == [("Fee notice", "event"), ("OS quiz", "exam")]
    assert ex.category == "general" and ex.importance == "medium"


def test_prompt_has_observed_at_not_today():
    p = json.loads(llm_extract.build_user_prompt("hello", OBS, "Asia/Kolkata"))
    assert p["OBSERVED_AT"] == "2026-10-16T10:20+05:30" and p["OBSERVED_WEEKDAY"] == "Friday"
    assert "against OBSERVED_AT" in llm_extract.SYSTEM_PROMPT and "Never invent dates" in llm_extract.SYSTEM_PROMPT


# ---------- date rules ----------

def test_resolve_keeps_valid_date():
    assert llm_extract.resolve_event(ev(), OBS, IST) == datetime(2026, 10, 20, 9, tzinfo=IST)


def test_drop_low_confidence_or_null():
    assert llm_extract.resolve_event(ev(confidence=0.4), OBS, IST) is None
    assert llm_extract.resolve_event(ev(due_at=None), OBS, IST) is None


def test_naive_gets_user_timezone():
    out = llm_extract.resolve_event(ev(due_at=datetime(2026, 10, 20, 9)), OBS, IST)
    assert out == datetime(2026, 10, 20, 9, tzinfo=IST)


def test_missing_year_means_next_occurrence():
    obs = datetime(2026, 12, 20, 10, tzinfo=IST)
    out = llm_extract.resolve_event(ev(date_text="5 Jan", due_at=datetime(2026, 1, 5, 9, tzinfo=IST)), obs, IST)
    assert out.date().isoformat() == "2027-01-05"
    # Explicit year is respected even if in the past.
    out = llm_extract.resolve_event(ev(date_text="5 Jan 2026", due_at=datetime(2026, 1, 5, 9, tzinfo=IST)), obs, IST)
    assert out.year == 2026


def test_all_day_times():
    exam = llm_extract.resolve_event(ev(all_day=True, due_at=datetime(2026, 10, 20, tzinfo=IST)), OBS, IST)
    assert exam.hour == 9
    dl = llm_extract.resolve_event(ev(type="deadline", all_day=True, due_at=datetime(2026, 10, 19, tzinfo=IST)), OBS, IST)
    assert (dl.hour, dl.minute) == (23, 59)


def test_absurd_dates_dropped():
    assert llm_extract.resolve_event(ev(date_text="2031", due_at=datetime(2031, 1, 1, tzinfo=IST)), OBS, IST) is None


# ---------- worker integration ----------

def test_worker_extracts_events_and_conflicts(conn, users):
    a, _ = users
    msgs = [
        ("DBMS midsem is on 21 Oct, 10am in LH-3", datetime(2026, 9, 25, 15, tzinfo=IST),
         "2026-10-21T10:00:00+05:30"),
        ("UPDATE: DBMS midsem postponed to 23 Oct, 2 pm", datetime(2026, 10, 14, 19, tzinfo=IST),
         "2026-10-23T14:00:00+05:30"),
    ]
    for text, when, _ in msgs:
        repo.ingest_source(conn, a, "telegram", repo.content_hash(text), [SegmentIn(text=text, observed_at=when)])
    repo.ingest_source(conn, a, "telegram", "short", [SegmentIn(text="ok 👍", observed_at=OBS)])
    replies = iter([
        payload([{"title": "DBMS midsem", "type": "exam", "date_text": "21 Oct, 10am", "due_at": msgs[0][2],
                  "all_day": False, "confidence": 0.95},
                 {"title": "Vague thing", "type": "event", "date_text": "soon", "due_at": None,
                  "all_day": True, "confidence": 0.3}], subject="DBMS"),
        payload([{"title": "DBMS midsem (rescheduled)", "type": "exam", "date_text": "23 Oct, 2 pm",
                  "due_at": msgs[1][2], "all_day": False, "confidence": 0.9}], subject="Database Management Systems"),
    ])
    llm = FakeLLM(lambda s, u: next(replies))
    assert worker.drain(conn, FakeEmbedder(), llm) == 3
    assert len(llm.prompts) == 2  # short "ok 👍" segment skipped
    events = repo.list_events(conn, a, statuses=("upcoming", "conflict", "past", "cancelled"))
    # The newer "UPDATE: ... postponed" message supersedes the old date (kept, not deleted).
    assert [(e.title, e.status) for e in events] == [("DBMS midsem", "cancelled"), ("DBMS midsem (rescheduled)", "upcoming")]
    assert events[0].superseded_by == events[1].id
    seg = repo.get_segment(conn, a, events[0].segment_id)
    assert seg.category == "exam" and seg.importance == "high" and seg.extracted_at


def test_llm_failure_retries_without_redoing_done_segments(conn, users):
    a, _ = users
    for t in ("First notice about the OS quiz on Monday", "Second notice about the CN lab on Friday"):
        repo.ingest_source(conn, a, "whatsapp", repo.content_hash(t), [SegmentIn(text=t, observed_at=OBS)])
    calls = {"n": 0}

    def flaky(system, user):
        calls["n"] += 1
        return "garbage" if "CN lab" in user and calls["n"] < 5 else payload()

    llm = FakeLLM(flaky)
    worker.run_once(conn, FakeEmbedder(), llm)  # job 1 ok
    worker.run_once(conn, FakeEmbedder(), llm)  # job 2: invalid JSON twice -> retry later
    job2 = repo.get_job(conn, a, 2)
    assert job2.status == "pending" and "LLMError" in job2.last_error and "1 segment(s)" in job2.last_error
    worker.run_once(conn, FakeEmbedder(), llm)
    assert repo.get_job(conn, a, 2).status == "done"
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2  # no duplicate indexing


def test_one_bad_segment_does_not_block_the_rest(conn, users):
    a, _ = users
    texts = ["Message one about the OS quiz on Monday", "Message two is the bad one here",
             "Message three about the CN viva on Friday"]
    repo.ingest_source(conn, a, "whatsapp", "wa", [SegmentIn(text=t, observed_at=OBS) for t in texts])
    llm = FakeLLM(lambda s, u: "garbage" if "bad one" in u else payload())
    worker.run_once(conn, FakeEmbedder(), llm)
    segs = repo.list_segments(conn, a, 1)
    assert [bool(s.extracted_at) for s in segs] == [True, False, True]
    assert repo.get_job(conn, a, 1).status == "pending"
    worker.run_once(conn, FakeEmbedder(), FakeLLM(payload()))  # retry only redoes segment 2
    assert all(s.extracted_at for s in repo.list_segments(conn, a, 1))


# ---------- deterministic relative dates (real failures seen with a 7B model) ----------

@pytest.mark.parametrize("text,obs,expected", [
    ("next Tuesday", datetime(2026, 10, 16, 10, tzinfo=IST), "2026-10-20"),  # Fri -> Tue
    ("next Tuesday 9 am", datetime(2026, 10, 16, 10, tzinfo=IST), "2026-10-20"),
    ("Saturday", datetime(2026, 10, 7, 12, tzinfo=IST), "2026-10-10"),  # Wed -> Sat
    ("on Friday", datetime(2026, 10, 16, 10, tzinfo=IST), "2026-10-23"),  # same weekday -> next week
    ("tomorrow", datetime(2026, 10, 11, 21, tzinfo=IST), "2026-10-12"),
    ("day after tomorrow", datetime(2026, 10, 11, 21, tzinfo=IST), "2026-10-13"),
    ("24 October (Saturday)", datetime(2026, 10, 7, 12, tzinfo=IST), None),  # explicit date wins
    ("the 19th", datetime(2026, 10, 17, 9, tzinfo=IST), None),
])
def test_relative_date(text, obs, expected):
    out = llm_extract.relative_date(text, obs.date())
    assert (out.isoformat() if out else None) == expected


def test_llm_weekday_mistake_is_corrected():
    wrong = ev(date_text="next Tuesday", due_at=datetime(2026, 10, 18, 9, tzinfo=IST))  # LLM said Sunday
    assert llm_extract.resolve_event(wrong, OBS, IST) == datetime(2026, 10, 20, 9, tzinfo=IST)


def test_midnight_without_time_is_all_day():
    e = ev(date_text="24 October (Saturday)", due_at=datetime(2026, 10, 24, 0, 0, tzinfo=IST), all_day=False)
    [out] = llm_extract.to_events(Extraction(summary="s", category="exam", importance="high", events=[e]),
                                  1, OBS, "Asia/Kolkata")
    assert out.all_day and out.due_at.hour == 9


def test_schedule_events_get_their_own_subject():
    raw = payload([
        {"title": "DBMS midsem", "type": "exam", "subject": "DBMS", "date_text": "21 October 2026 10:00 am",
         "due_at": "2026-10-21T10:00:00+05:30", "confidence": 0.9},
        {"title": "OS midsem", "type": "exam", "subject": None, "date_text": "22 October 2026 10:00 am",
         "due_at": "2026-10-22T10:00:00+05:30", "confidence": 0.9},
    ], subject="CS301, CS302")
    events = llm_extract.to_events(parse_json_model(raw, Extraction), 1, OBS, "Asia/Kolkata")
    assert [e.subject for e in events] == ["DBMS", None]  # combined segment subject is never used


def test_deadline_with_llm_seconds_but_no_time_is_all_day():
    e = ev(type="deadline", title="Hackathon registration", date_text="18 October",
           due_at=datetime(2026, 10, 18, 23, 59, 59, tzinfo=IST))
    [out] = llm_extract.to_events(Extraction(events=[e]), 1, OBS, "Asia/Kolkata")
    assert out.all_day and (out.due_at.hour, out.due_at.minute) == (23, 59)


@pytest.mark.parametrize("text,obs,event_hour,expected", [
    ("saturday 3pm", datetime(2026, 10, 17, 13, 8, tzinfo=IST), 15, "2026-10-17"),  # Sat 1pm -> today
    ("saturday 3pm", datetime(2026, 10, 17, 17, 0, tzinfo=IST), 15, "2026-10-24"),  # already past -> next week
    ("next saturday 3pm", datetime(2026, 10, 17, 13, 0, tzinfo=IST), 15, "2026-10-24"),
    ("on saturday", datetime(2026, 10, 17, 13, 0, tzinfo=IST), 9, "2026-10-24"),  # no time given
])
def test_same_weekday_later_today(text, obs, event_hour, expected):
    from datetime import time as t
    out = llm_extract.relative_date(text, obs.date(), obs.time(), t(event_hour, 0))
    assert out.isoformat() == expected
