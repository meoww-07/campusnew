"""The Today feed: what matters right now. Shared by GET /api/today and the 07:30 Telegram digest.

Sections (empty ones omitted, each capped, `total` says how many exist for "See all"):
  due_soon    events due in the next 48h, soonest first, with countdown + reminder state
  attention   conflicts to confirm, failed processing, low-confidence dates
  new         high/medium items added since the user last looked (high first, then newest)
  week        events 48h..7d out, grouped by local day
  spaces      latest 2 items of each pinned space
  rediscover  1-2 high-importance resources saved > 7 days ago and never opened
Snoozed items are hidden until their snooze ends.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app import repo
from app.models import User
from app.organize import spaces as spaces_service
from app.retrieve.events_view import LOW_CONFIDENCE, event_payloads

DUE_SOON = timedelta(hours=48)
WEEK = timedelta(days=7)
REDISCOVER_AFTER = timedelta(days=7)
CAPS = {"due_soon": 8, "attention": 6, "new": 6, "week": 12, "spaces": 2, "rediscover": 2}
IMPORTANCE_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}


def snooze_until(user: User, now: datetime | None = None) -> datetime:
    """'Snooze to tomorrow' = tomorrow 07:00 in the user's timezone."""
    tz = ZoneInfo(user.timezone)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    return datetime.combine(local.date() + timedelta(days=1), time(7, 0), tz)


def _greeting(user: User, now: datetime) -> str:
    hour = now.astimezone(ZoneInfo(user.timezone)).hour
    part = "morning" if 4 <= hour < 12 else "afternoon" if hour < 17 else "evening"
    first = (user.name or "").split(" ")[0]
    return f"Good {part}{', ' + first if first else ''}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary_line(due: list[dict], attention: list[dict], new_total: int) -> str:
    """'2 deadlines in the next 48 hours and 1 conflict to check.' (counts only, no LLM)."""
    parts = []
    by_type = {t: sum(1 for e in due if e["type"] == t) for t in ("exam", "deadline", "event")}
    soon = [_plural(n, w) for w, n in (("exam", by_type["exam"]), ("deadline", by_type["deadline"]),
                                        ("event", by_type["event"])) if n]
    if soon:
        parts.append(_join(soon) + " in the next 48 hours")
    conflicts = sum(1 for a in attention if a["kind"] == "conflict")
    failed = sum(1 for a in attention if a["kind"] == "failed_job")
    unsure = sum(1 for a in attention if a["kind"] == "low_confidence")
    if conflicts:
        parts.append(_plural(conflicts, "conflict") + " to check")
    if unsure:
        parts.append(_plural(unsure, "date") + " to confirm")
    if failed:
        parts.append(_plural(failed, "upload") + " that failed")
    if new_total:
        parts.append(_plural(new_total, "new item"))
    if not parts:
        return "You're all caught up."
    text = _join(parts)
    return text[0].upper() + text[1:] + "."


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _day_label(day, today) -> str:
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    return day.strftime("%a %d %b")


def build_today(conn: sqlite3.Connection, user: User, now: datetime | None = None, *,
                since: datetime | None = None, new_importances: tuple[str, ...] = ("high", "medium")) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(user.timezone)
    if since is None:
        since = datetime.fromisoformat(user.last_seen_today_at) if user.last_seen_today_at else now - timedelta(hours=24)
    snoozed = repo.active_snoozes(conn, user.id, now)
    visible = lambda kind, item_id: (kind, item_id) not in snoozed  # noqa: E731

    # 1 + 4: events by horizon
    horizon = [e for e in repo.list_events_range(conn, user.id, now, now + WEEK, statuses=("upcoming", "conflict"))
               if visible("event", e.id) and datetime.fromisoformat(e.due_at) >= now]
    cards = event_payloads(conn, user.id, horizon, now)
    due = [c for c in cards if c["seconds_until"] <= DUE_SOON.total_seconds()]
    later = [c for c in cards if c["seconds_until"] > DUE_SOON.total_seconds()]

    # 2: needs attention
    attention: list[dict] = []
    seen_conflicts: set[int] = set()
    for c in cards:
        if c["status"] == "conflict" and c["id"] not in seen_conflicts:
            partners = repo.conflict_partners(conn, user.id, c["id"])
            group = [c] + event_payloads(conn, user.id, partners, now)
            seen_conflicts |= {g["id"] for g in group}
            attention.append({"kind": "conflict", "title": c["title"], "events": group, "due_at": c["due_at"]})
    for c in cards:
        if c["low_confidence"] and c["status"] != "conflict":
            attention.append({"kind": "low_confidence", "title": c["title"], "events": [c], "due_at": c["due_at"]})
    for j in repo.failed_jobs(conn, user.id):
        attention.append({"kind": "failed_job", "title": j["source_title"] or j["source_type"], "job": j,
                          "due_at": None})
    order = {"conflict": 0, "low_confidence": 1, "failed_job": 2}
    attention.sort(key=lambda a: (order[a["kind"]], a["due_at"] or "9999"))

    # 3: new since last look
    new_items = [s for s in repo.segments_added_since(conn, user.id, since, new_importances)
                 if visible("segment", s["segment_id"])]

    # 5: pinned spaces
    pinned = [s for s in spaces_service.list_overview(conn, user.id) if s["pinned"]]
    space_sections = []
    for sp in pinned:
        items, _ = repo.list_space_items(conn, user.id, sp["id"], limit=CAPS["spaces"])
        items = [i for i in items if visible("segment", i["segment_id"])]
        if items:
            space_sections.append({"space": {k: sp[k] for k in ("id", "name", "icon", "color", "item_count")},
                                   "items": items})

    # 6: rediscover (rotates daily through the candidates)
    candidates = [s for s in repo.rediscover_candidates(conn, user.id, now - REDISCOVER_AFTER)
                  if visible("segment", s["segment_id"])]
    if candidates:
        start = now.astimezone(tz).toordinal() % len(candidates)
        candidates = (candidates[start:] + candidates[:start])[:CAPS["rediscover"]]

    # 4: this week, grouped by local day
    today_local = now.astimezone(tz).date()
    days: dict[str, dict] = {}
    for c in later[:CAPS["week"]]:
        day = datetime.fromisoformat(c["due_at"]).astimezone(tz).date()
        days.setdefault(day.isoformat(), {"date": day.isoformat(), "label": _day_label(day, today_local),
                                          "events": []})["events"].append(c)

    sections = []

    def add(key: str, title: str, items: list, total: int, see_all: str | None) -> None:
        if items:
            sections.append({"key": key, "title": title, "items": items, "total": total, "see_all": see_all})

    add("due_soon", "Due soon", due[:CAPS["due_soon"]], len(due), "#/calendar?view=list")
    add("attention", "Needs your attention", attention[:CAPS["attention"]], len(attention), None)
    add("new", "New since you last looked", new_items[:CAPS["new"]], len(new_items), "#/sources")
    add("week", "This week", list(days.values()), len(later), "#/calendar?view=week")
    add("spaces", "From your spaces", space_sections, len(space_sections), "#/spaces")
    add("rediscover", "Rediscover", candidates, len(candidates), None)
    return {
        "greeting": _greeting(user, now),
        "summary": summary_line(due, attention, len(new_items)),
        "counts": {"due_soon": len(due), "attention": len(attention), "new": len(new_items), "week": len(later)},
        "since": repo.iso(since),
        "generated_at": repo.iso(now),
        "sections": sections,
    }


def render_digest(feed: dict[str, Any], tz_name: str) -> str | None:
    """Telegram text for the 07:30 digest, from the same feed. None if there's nothing to say."""
    tz = ZoneInfo(tz_name)
    by_key = {s["key"]: s for s in feed["sections"]}
    due, attention, new = by_key.get("due_soon"), by_key.get("attention"), by_key.get("new")
    if not (due or attention or new):
        return None
    now = datetime.fromisoformat(feed["generated_at"]).astimezone(tz)
    lines = [f"☀️ {feed['greeting']}! Your CampusMind digest for {now.strftime('%a %d %b')}", feed["summary"]]
    if due:
        groups: dict[str, list[dict]] = {}
        for e in due["items"]:
            day = datetime.fromisoformat(e["due_at"]).astimezone(tz).date()
            groups.setdefault(_day_label(day, now.date()), []).append(e)
        for label, events in groups.items():
            lines.append(f"\n{label}:")
            for e in events:
                when = "" if e["all_day"] else datetime.fromisoformat(e["due_at"]).astimezone(tz).strftime(
                    ", %I:%M %p").replace(" 0", " ")
                flag = " ⚠️ conflicting dates" if e["status"] == "conflict" else ""
                moved = " 🔁 date changed" if e["replaces"] else ""
                lines.append(f"• {e['title']} ({e['type']}) — {datetime.fromisoformat(e['due_at']).astimezone(tz).strftime('%a %d %b')}{when}{flag}{moved}")
    if attention:
        conflicts = [a for a in attention["items"] if a["kind"] == "conflict"]
        if conflicts:
            lines.append("\nNeeds your attention:")
            lines += [f"• {a['title']}: sources disagree on the date" for a in conflicts]
    if new:
        lines.append("\nNew important items:")
        lines += [f"• {s['summary'] or s['text'][:120]}" for s in new["items"][:5]]
    return "\n".join(lines)


def seconds_label(seconds: int) -> str:
    """Countdown text: 'in 3h 20m', 'in 2 days'."""
    if seconds < 0:
        return "overdue"
    minutes = seconds // 60
    if minutes < 60:
        return f"in {minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"in {hours}h {mins}m" if mins else f"in {hours}h"
    days = hours // 24
    return f"in {days} day{'s' if days != 1 else ''}"


__all__ = ["build_today", "render_digest", "snooze_until", "summary_line", "seconds_label", "LOW_CONFIDENCE"]
