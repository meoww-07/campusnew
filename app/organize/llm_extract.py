"""Structured extraction (summary, category, events) with date resolution.

The LLM resolves dates against observed_at; deterministic post-processing
then enforces the rules it may get wrong:
  * naive datetimes get the user's timezone
  * a date written without a year that lands before observed_at is moved to
    the next occurrence (next year)
  * all-day items get a fixed local time (09:00 exams/events, 23:59 deadlines)
  * low-confidence or undated events are dropped; absurd dates are dropped
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.llm import LLM, chat_json
from app.models import EventIn, ExtractedEvent, Extraction

MIN_SEGMENT_CHARS = 15
MIN_CONFIDENCE = 0.5

SYSTEM_PROMPT = """You extract structured information from a college student's saved messages, emails and notices.
Return ONLY a JSON object with exactly these keys:
{
  "summary": "one short sentence",
  "category": "exam" | "deadline" | "event" | "notice" | "resource" | "placement" | "general",
  "subject": "short course name as students write it (e.g. DBMS, OS, Compiler Design) or null",
  "importance": "high" | "medium" | "low",
  "events": [
    {"title": "short title incl. subject and kind, e.g. 'DBMS midsem' or 'OS Assignment 3'",
     "type": "exam" | "deadline" | "event",
     "subject": "the ONE course this event belongs to (e.g. DBMS), or null",
     "date_text": "the exact words in the text that give the date/time",
     "due_at": "ISO 8601 datetime WITH timezone offset, e.g. 2026-10-23T14:00:00+05:30, or null",
     "all_day": true if no time of day is given,
     "confidence": number between 0 and 1}
  ]
}
Rules:
- Resolve relative dates ("tomorrow", "next Tuesday", "this Friday") against OBSERVED_AT (when the message was written), NOT against today.
- If the year is missing, use the next occurrence of that date on or after OBSERVED_AT.
- Use the user's timezone for the offset.
- Never invent dates. If no specific date is stated, do not create an event (or set due_at null).
- An event needs a real date in the text. Past references ("was held", "yesterday's quiz") are not new events.
- If a date was changed (postponed/rescheduled), output only the NEW date as the event.
- Only real academic/admin items: exams, quizzes, vivas, assignment/form/fee deadlines, reviews,
  registrations, extra classes. Not references to documents ("check the PDF"), chit-chat or plans between friends.
- A schedule/table with several courses produces one event per course, each with its own subject.
- Return "events": [] if there are none.
- category "placement" ONLY for job/internship recruiting (drives, company tests, offers). Hackathons and competitions are "event".
- importance "high": exams, deadlines, schedule changes, fees; "low": chit-chat.
- "summary" says what the item IS and states its key facts in your own words, written from the TEXT only
  (for example, a fee circular -> "Accounts circular: tuition fee due 31 Oct, late fee Rs 500/week").
  Never just the caption, sender or file name ("Photo from X" is not a summary). Never copy this example.
- If SOURCE says the text was read by OCR, expect recognition errors ("crtena" = "criteria", "leamable" = "learnable")
  and read through them; never copy an obviously misread word into a title or summary."""


def build_user_prompt(text: str, observed_at: datetime, tz_name: str, source: str | None = None) -> str:
    local = observed_at.astimezone(ZoneInfo(tz_name))
    return json.dumps({
        **({"SOURCE": source} if source else {}),
        "OBSERVED_AT": local.isoformat(timespec="minutes"),
        "OBSERVED_WEEKDAY": local.strftime("%A"),
        "USER_TIMEZONE": tz_name,
        "TEXT": text[:6000],
    }, ensure_ascii=False, indent=1)


_HAS_YEAR = re.compile(r"\b(19|20)\d{2}\b|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|'\d{2}\b")
_HAS_TIME = re.compile(r"\b\d{1,2}(:\d{2})?\s*(a\.?m\.?|p\.?m\.?)|\b\d{1,2}:\d{2}\b|\bnoon\b|\bmidnight\b", re.I)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAY = re.compile(r"\b(?:(this|next|coming)\s+)?(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\b", re.I)
_DAY_OF_MONTH = re.compile(r"\b\d{1,2}(st|nd|rd|th)?\b(?!\s*(:|am|pm|a\.m|p\.m))", re.I)


def relative_date(date_text: str | None, observed: date, observed_time: time | None = None,
                  event_time: time | None = None) -> date | None:
    """Deterministic resolution of purely relative phrases against the message date.

    Handles today/tonight/tomorrow/day after tomorrow and weekday names
    ("next Tuesday", "on Saturday"). Returns None when the text has an
    explicit day of month (then the LLM's absolute date is trusted).
    """
    t = (date_text or "").lower()
    if not t or _DAY_OF_MONTH.search(_HAS_TIME.sub(" ", t)):
        return None
    if "day after tomorrow" in t:
        return observed + timedelta(days=2)
    if "tomorrow" in t:
        return observed + timedelta(days=1)
    if re.search(r"\b(today|tonight)\b", t):
        return observed
    m = _WEEKDAY.search(t)
    if not m:
        return None
    target = next(i for i, d in enumerate(_WEEKDAYS) if d.startswith(m.group(2).lower()[:3]))
    ahead = (target - observed.weekday()) % 7
    if ahead == 0:
        # Same weekday: "Saturday 3pm" said on Saturday at 1pm means today; said at 5pm,
        # or "next Saturday", or with no time given, it means next week.
        later_today = (event_time is not None and observed_time is not None and event_time > observed_time
                       and _HAS_TIME.search(t) is not None)
        if (m.group(1) or "").lower() == "next" or not later_today:
            ahead = 7
    return observed + timedelta(days=ahead)


def resolve_event(ev: ExtractedEvent, observed_at: datetime, tz: ZoneInfo) -> datetime | None:
    if ev.due_at is None or ev.confidence < MIN_CONFIDENCE:
        return None
    due = ev.due_at if ev.due_at.tzinfo else ev.due_at.replace(tzinfo=tz)
    local = due.astimezone(tz)
    obs = observed_at.astimezone(tz)
    rel = relative_date(ev.date_text, obs.date(), obs.time(), local.time())
    if rel is not None and rel != local.date():
        local = datetime.combine(rel, local.timetz())  # LLMs often miscount weekdays
    no_time_given = not _HAS_TIME.search(ev.date_text or "")
    all_day = ev.all_day or (no_time_given and (local.hour, local.minute) in ((0, 0), (23, 59)))
    if all_day:
        local = datetime.combine(local.date(), time(23, 59) if ev.type == "deadline" else time(9, 0), tz)
    if local < obs - timedelta(days=1) and not _HAS_YEAR.search(ev.date_text or ""):
        try:
            bumped = local.replace(year=local.year + 1)
        except ValueError:  # 29 Feb
            bumped = local + timedelta(days=365)
        if bumped >= obs - timedelta(days=1):
            local = bumped
    if not (obs - timedelta(days=400) <= local <= obs + timedelta(days=730)):
        return None  # far outside a plausible window: likely hallucinated
    return local


def extract(llm: LLM, text: str, observed_at: datetime, tz_name: str, source: str | None = None) -> Extraction:
    """Raises app.llm.LLMError if the model returns invalid JSON twice."""
    return chat_json(llm, SYSTEM_PROMPT, build_user_prompt(text, observed_at, tz_name, source), Extraction)


def to_events(extraction: Extraction, segment_id: int, observed_at: datetime, tz_name: str) -> list[EventIn]:
    tz = ZoneInfo(tz_name)
    segment_subject = extraction.subject if extraction.subject and not is_multi_subject(extraction.subject) else None
    out: list[EventIn] = []
    for ev in extraction.events:
        due = resolve_event(ev, observed_at, tz)
        if due is None:
            continue
        all_day = ev.all_day or ((due.hour, due.minute) in ((9, 0), (23, 59))
                                 and not _HAS_TIME.search(ev.date_text or ""))
        out.append(EventIn(
            segment_id=segment_id, title=ev.title.strip()[:200], type=ev.type,
            subject=ev.subject or segment_subject, due_at=due, all_day=all_day,
            date_text=ev.date_text, confidence=ev.confidence,
        ))
    return out


def is_multi_subject(subject: str) -> bool:
    return bool(re.search(r",|/|&|\band\b", subject))
