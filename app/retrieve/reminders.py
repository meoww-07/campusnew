"""Scheduled jobs: deadline reminders, daily digest, event lifecycle.

APScheduler runs in Asia/Kolkata inside the FastAPI event loop:
  every 5 min   24h / 2h reminders for upcoming+conflict events
  daily 07:30   digest of today+tomorrow events and new high-importance items
  every 15 min  mark events whose valid_until has passed as 'past'

A delivery row is inserted BEFORE sending (UNIQUE(event_id, kind) makes a
second insert a no-op, so nothing is sent twice). If the send fails the row
is removed again so the next run retries.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import repo
from app.retrieve import answer
from app.models import Event, User

log = logging.getLogger(__name__)

Sender = Callable[[int, str], Awaitable[None]]  # (telegram_chat_id, text)
SCHED_TZ = "Asia/Kolkata"
WINDOWS = {"2h": timedelta(hours=2), "24h": timedelta(hours=24)}


def reminder_kind(event: Event, now: datetime) -> str | None:
    left = datetime.fromisoformat(event.due_at) - now
    if left <= timedelta(0):
        return None
    if left <= WINDOWS["2h"]:
        return "2h"
    if left <= WINDOWS["24h"]:
        return "24h"
    return None


def _when(event: Event, tz_name: str) -> str:
    due = datetime.fromisoformat(event.due_at).astimezone(ZoneInfo(tz_name))
    return due.strftime("%a %d %b") + ("" if event.all_day else due.strftime(", %I:%M %p").replace(" 0", " "))


def format_reminder(event: Event, kind: str, tz_name: str, moved: str | None = None) -> str:
    lead = "⏰ In about 2 hours" if kind == "2h" else "📅 Tomorrow-ish (within 24h)"
    text = f"{lead}: {event.title} ({event.type}) — {_when(event, tz_name)}"
    if event.status == "conflict":
        text += "\n⚠️ Your sources disagree on this date. Ask me (/ask) to see both."
    if moved:
        text += f"\n🔁 Date changed: {moved}."
    return text


async def send_due_reminders(conn: sqlite3.Connection, send: Sender, now: datetime | None = None) -> int:
    """Send pending 24h/2h reminders for every user. Returns reminders sent."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    for user_id in repo.list_user_ids(conn):
        user = repo.get_user(conn, user_id)
        for ev in repo.list_events(conn, user_id, start=now, end=now + WINDOWS["24h"]):
            kind = reminder_kind(ev, now)
            if kind is None or not repo.claim_reminder(conn, user_id, ev.id, kind):
                continue
            if kind == "2h":
                repo.claim_reminder(conn, user_id, ev.id, "24h")  # too late for the 24h one; never send it
            try:
                moved = answer.moved_from(conn, user_id, [ev], user.timezone).get(ev.id)
                await send(user.telegram_chat_id, format_reminder(ev, kind, user.timezone, moved))
            except Exception:  # noqa: BLE001 - retried on the next run
                log.exception("reminder send failed (event %s, %s)", ev.id, kind)
                repo.release_reminder(conn, user_id, ev.id, kind)
            else:
                sent += 1
    return sent


def build_digest(conn: sqlite3.Connection, user: User, now: datetime | None = None) -> str | None:
    """The 07:30 digest: the same Today feed the website shows (due in 48h, conflicts,
    new high-importance items from the last 24h), as text. None if empty."""
    from app.retrieve.today import build_today, render_digest
    now = now or datetime.now(timezone.utc)
    feed = build_today(conn, user, now, since=now - timedelta(hours=24), new_importances=("high",))
    return render_digest(feed, user.timezone)


async def send_digests(conn: sqlite3.Connection, send: Sender, now: datetime | None = None) -> int:
    sent = 0
    for user_id in repo.list_user_ids(conn):
        user = repo.get_user(conn, user_id)
        text = build_digest(conn, user, now)
        if not text:
            continue
        try:
            await send(user.telegram_chat_id, text)
            sent += 1
        except Exception:  # noqa: BLE001
            log.exception("digest send failed for user %s", user_id)
    return sent


async def mark_past(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    return sum(repo.mark_past_events(conn, uid, now) for uid in repo.list_user_ids(conn))


def build_scheduler(conn: sqlite3.Connection, send: Sender) -> AsyncIOScheduler:
    """Scheduler with the reminder jobs. Call .start() inside the running event loop."""
    sched = AsyncIOScheduler(timezone=SCHED_TZ, job_defaults={"coalesce": True, "max_instances": 1,
                                                               "misfire_grace_time": 300})
    sched.add_job(send_due_reminders, IntervalTrigger(minutes=5), args=[conn, send], id="reminders",
                  next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20))
    sched.add_job(send_digests, CronTrigger(hour=7, minute=30, timezone=SCHED_TZ), args=[conn, send], id="digest")
    sched.add_job(mark_past, IntervalTrigger(minutes=15), args=[conn], id="lifecycle",
                  next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10))
    return sched
