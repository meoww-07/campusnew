"""WhatsApp "Export chat" (.txt, without media) parser.

Handles Android lines like
    12/10/26, 9:15 pm - Sender: text
    12/10/2026, 21:15 - Sender: text
    12/10/26, 9:15 PM - Sender: text      (narrow no-break space)
and iOS lines like
    [12/10/26, 9:15:03 PM] Sender: text
Dates are dd/mm/yy or dd/mm/yyyy. Lines without a date prefix continue the
previous message. System messages and media placeholders are skipped.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import repo
from app.models import IngestResult, SegmentIn

IST = ZoneInfo("Asia/Kolkata")
_SPACES = "    "
_LINE = re.compile(
    r"^[‎‏]?\[?(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{2}|\d{4}),?[" + _SPACES + r"]+"
    r"(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?"
    r"(?:[" + _SPACES + r"]*(?P<ampm>[AaPp]\.?[" + _SPACES + r"]?[Mm]\.?))?"
    r"\]?[" + _SPACES + r"]*(?:[-–][" + _SPACES + r"]*)?(?P<rest>.*)$"
)
SKIP_BODIES = (
    "<media omitted>", "this message was deleted", "you deleted this message", "null",
    "<attached:", "image omitted", "video omitted", "sticker omitted", "audio omitted",
    "document omitted", "gif omitted", "‎image omitted", "waiting for this message",
)


@dataclass(frozen=True)
class WaMessage:
    timestamp: datetime  # aware, Asia/Kolkata
    sender: str
    text: str
    line_no: int  # 1-based line where the message starts


def _timestamp(m: re.Match[str], tz: ZoneInfo) -> datetime | None:
    day, month, year = int(m["d"]), int(m["m"]), int(m["y"])
    if year < 100:
        year += 2000
    hour, minute, second = int(m["H"]), int(m["M"]), int(m["S"] or 0)
    if m["ampm"]:
        pm = m["ampm"].lower().startswith("p")
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if pm else 0)
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=tz)
    except ValueError:
        return None


def _is_skippable(text: str) -> bool:
    t = text.strip().lower().lstrip("‎")
    return not t or any(t.startswith(s) for s in SKIP_BODIES)


def parse(text: str, tz: ZoneInfo = IST) -> list[WaMessage]:
    messages: list[WaMessage] = []
    current: dict | None = None  # {"ts", "sender", "lines", "line_no"} or {"system": True}

    def flush() -> None:
        if current and not current.get("system"):
            body = "\n".join(current["lines"]).strip()
            if not _is_skippable(body):
                messages.append(WaMessage(current["ts"], current["sender"], body, current["line_no"]))

    for line_no, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        line = raw.lstrip("﻿")
        m = _LINE.match(line)
        ts = _timestamp(m, tz) if m else None
        if m and ts is not None:
            flush()
            rest = m["rest"]
            sender, sep, body = rest.partition(": ")
            if not sep or not sender.strip() or len(sender) > 60:
                current = {"system": True}  # "X added Y", encryption notice, ...
            else:
                current = {"ts": ts, "sender": sender.strip().lstrip("‎~ "), "lines": [body], "line_no": line_no}
        elif current is not None and not current.get("system"):
            current["lines"].append(line)
        # continuation of a system message (or preamble) -> ignored
    flush()
    return messages


def chat_name_from_filename(filename: str | None) -> str:
    stem = Path(filename or "WhatsApp chat").stem
    m = re.match(r"WhatsApp Chat (?:with|-) (.+)", stem, re.IGNORECASE)
    return (m.group(1) if m else stem).strip()


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def import_export(
    conn: sqlite3.Connection, user_id: int, data: bytes, filename: str | None, *, tz: ZoneInfo = IST,
) -> IngestResult | None:
    """Parse and store a chat export as one source with one segment per message.

    Returns None if the file contains no recognizable WhatsApp messages.
    """
    chat_name = chat_name_from_filename(filename)
    msgs = parse(decode(data), tz)
    if not msgs:
        return None
    segments = [
        SegmentIn(text=m.text, observed_at=m.timestamp,
                  locator={"chat_name": chat_name, "sender": m.sender, "line_no": m.line_no})
        for m in msgs
    ]
    return repo.ingest_source(
        conn, user_id, "whatsapp", repo.content_hash(data), segments,
        title=f"WhatsApp: {chat_name}", external_id=filename,
    )
