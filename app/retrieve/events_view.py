"""One JSON shape for events everywhere (calendar, Today, events API)."""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from app import repo
from app.models import Event

TYPE_COLORS = {"exam": "#c62828", "deadline": "#ef6c00", "event": "#3d5afe"}
LOW_CONFIDENCE = 0.7


def event_payloads(conn: sqlite3.Connection, user_id: int, events: Sequence[Event],
                   now: datetime | None = None) -> list[dict[str, Any]]:
    """Events + source snippet, spaces, color, what they replaced, reminders sent, urgency flags."""
    now = now or datetime.now(timezone.utc)
    seg_ids = [e.segment_id for e in events if e.segment_id]
    briefs = repo.segment_brief(conn, user_id, seg_ids)
    in_spaces = repo.spaces_of_segments(conn, user_id, seg_ids)
    replaced = repo.superseded_by_map(conn, user_id, [e.id for e in events])
    sent = repo.reminders_sent(conn, user_id, [e.id for e in events])
    out = []
    for e in events:
        due = datetime.fromisoformat(e.due_at)
        space_list = in_spaces.get(e.segment_id, []) if e.segment_id else []
        subject_space = next((s for s in space_list if s.get("color")), None)
        brief = briefs.get(e.segment_id) if e.segment_id else None
        out.append({
            **e.model_dump(),
            "color": TYPE_COLORS.get(e.type, "#607d8b"),
            "space_color": subject_space["color"] if subject_space else None,
            "spaces": space_list,
            "source": brief["source"] if brief else None,
            "snippet": brief["text"][:400] if brief else None,
            "observed_at": brief["observed_at"] if brief else None,
            "locator": brief["locator"] if brief else None,
            "replaces": [{"id": o.id, "due_at": o.due_at, "all_day": o.all_day} for o in replaced.get(e.id, [])],
            "reminders_sent": sent.get(e.id, []),
            "seconds_until": int((due - now).total_seconds()),
            "due_within_24h": timedelta(0) <= due - now <= timedelta(hours=24),
            "low_confidence": (not e.user_edited) and e.confidence < LOW_CONFIDENCE,
            "manual": e.segment_id is None,
        })
    return out
