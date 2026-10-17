"""Read-only iCalendar (RFC 5545) feed of a user's events, for Google Calendar subscriptions."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models import Event

PRODID = "-//CampusMind//Calendar Feed//EN"


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Lines longer than 75 octets continue on the next line after a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += b
    parts.append(chunk.decode("utf-8"))
    return "\r\n ".join(parts)


def _utc(iso_ts: str) -> str:
    return datetime.fromisoformat(iso_ts).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(events: Sequence[Event], tz_name: str, calendar_name: str = "CampusMind",
              base_url: str = "", now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(tz_name)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_escape(calendar_name)}", f"X-WR-TIMEZONE:{tz_name}",
             "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for e in events:
        if e.status == "cancelled":
            continue
        summary = e.title + (" (⚠ conflicting dates)" if e.status == "conflict" else "") + \
            (" ✓" if e.status == "done" else "")
        desc = [f"Type: {e.type}"] + ([f"Subject: {e.subject}"] if e.subject else [])
        if e.status == "conflict":
            desc.append("Your sources give different dates for this. Check CampusMind.")
        if e.date_text:
            desc.append(f"From the source: \u201c{e.date_text}\u201d")
        lines += ["BEGIN:VEVENT", f"UID:event-{e.id}@campusmind", f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}",
                  f"SEQUENCE:{e.version}"]
        if e.all_day:
            day = datetime.fromisoformat(e.due_at).astimezone(tz).date()
            last = datetime.fromisoformat(e.end_at).astimezone(tz).date() if e.end_at else day
            lines += [f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
                      f"DTEND;VALUE=DATE:{(last + timedelta(days=1)).strftime('%Y%m%d')}"]
        else:
            end = e.end_at or (datetime.fromisoformat(e.due_at) + timedelta(hours=1)).isoformat()
            lines += [f"DTSTART:{_utc(e.due_at)}", f"DTEND:{_utc(end)}"]
        lines += [f"SUMMARY:{_escape(summary)}", f"DESCRIPTION:{_escape(chr(10).join(desc))}"]
        if e.location:
            lines.append(f"LOCATION:{_escape(e.location)}")
        if base_url:
            lines.append(f"URL:{base_url}/#/calendar?event={e.id}")
        lines += [f"STATUS:{'TENTATIVE' if e.status == 'conflict' else 'CONFIRMED'}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
