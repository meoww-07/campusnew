"""Question answering over the user's saved info, with [n] citations."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app import repo
from app.llm import LLM
from app.models import ChunkHit, Event
from app.organize.indexer import Embedder
from app.search.hybrid import STOPWORDS, search

TOP_K = 8
NOT_FOUND = "I couldn't find that in your saved info."

SYSTEM_PROMPT = f"""You are CampusMind, a student's personal memory assistant.
Answer ONLY using the numbered context blocks provided. Rules:
- Cite every factual claim with its block number in square brackets, e.g. [2]. Use only numbers that exist.
- If sources conflict (e.g. an exam was rescheduled), show both versions with their dates and sources, and say which is newer.
- When asked for the "latest"/"current" info, prefer the newest source by date.
- Relative dates inside a source ("tomorrow", "next Tuesday") are relative to THAT source's date in its header,
  not to today. Prefer the resolved dates listed under KNOWN EVENTS.
- Never invent dates, times, places or facts. Do not use outside knowledge.
- Blocks can be OCR text from photos/scans (expect typos like "crtena" for "criteria") and tables, where a number
  next to an item is usually its marks, weight, score or count. Read them sensibly; related wording counts
  (e.g. a judging rubric answers "how many marks for X", "evaluation criteria", "how is it judged").
- Only if NO block is relevant to the question, reply exactly: "{NOT_FOUND}". If a block partly answers it,
  answer that part and say what is missing.
- Be concise: a few short sentences or bullets."""


class Citation(BaseModel):
    n: int
    chunk_id: int
    source_id: int
    segment_id: int | None = None
    source_type: str
    label: str  # human-readable header, e.g. "WhatsApp · CSE-A · Riya · 14 Oct 2026"
    snippet: str
    observed_at: str
    locator: dict[str, Any]


class Answer(BaseModel):
    answer: str
    citations: list[Citation]  # only blocks the answer actually cites
    context: list[Citation]  # every block shown to the LLM


def fmt_time(iso_ts: str, tz_name: str) -> str:
    dt = datetime.fromisoformat(iso_ts).astimezone(ZoneInfo(tz_name))
    return dt.strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " ")


def fmt_date(iso_ts: str, tz_name: str) -> str:
    return datetime.fromisoformat(iso_ts).astimezone(ZoneInfo(tz_name)).strftime("%a %d %b %Y")


def describe_segment(brief: dict, tz_name: str) -> str:
    """describe() for a repo.segment_brief() dict (no chunk involved)."""
    return describe(ChunkHit(chunk_id=0, text=brief["text"], segment_id=brief["segment_id"], locator=brief["locator"],
                             observed_at=brief["observed_at"], source_id=brief["source"]["id"],
                             source_type=brief["source"]["type"], source_title=brief["source"]["title"]), tz_name)


def describe(hit: ChunkHit, tz_name: str) -> str:
    loc = hit.locator
    parts: list[str] = []
    if hit.source_type == "whatsapp":
        parts += ["WhatsApp", loc.get("chat_name"), loc.get("sender")]
    elif hit.source_type == "gmail":
        parts += ["Email", loc.get("subject"), f"from {loc['from']}" if loc.get("from") else None]
        if loc.get("attachment"):
            parts.append(f"attachment {loc['attachment']}")
    elif hit.source_type == "telegram":
        parts.append("Telegram")
        if loc.get("forward_sender"):
            parts.append(f"fwd from {loc['forward_sender']}")
        if loc.get("file_name"):
            parts.append(loc["file_name"])
    else:
        parts += ["Upload", hit.source_title]
    if loc.get("page"):
        parts.append(f"page {loc['page']}")
    parts.append(fmt_time(hit.observed_at, tz_name))
    return " · ".join(p for p in parts if p)


def _question_terms(question: str) -> set[str]:
    return {w for w in re.findall(r"\w+", question.lower()) if w not in STOPWORDS and len(w) > 2}


def relevant_events(conn: sqlite3.Connection, user_id: int, question: str, segment_ids: set[int]) -> list[Event]:
    """Upcoming/conflict events tied to retrieved segments or matching question words."""
    terms = _question_terms(question)
    out = []
    for e in repo.list_events(conn, user_id, statuses=("upcoming", "conflict")):
        words = set(re.findall(r"\w+", f"{e.title} {e.subject or ''}".lower()))
        if e.segment_id in segment_ids or terms & words:
            out.append(e)
    return out[:10]


FOLLOWUP_WINDOW = timedelta(minutes=30)


def _chat_followup(conn: sqlite3.Connection, user_id: int, hit: ChunkHit) -> str:
    """Append the next chat message if it came soon after (the answer to a question)."""
    nxt = repo.next_segment(conn, user_id, hit.segment_id)
    if nxt is None:
        return ""
    gap = datetime.fromisoformat(nxt.observed_at) - datetime.fromisoformat(hit.observed_at)
    if gap > FOLLOWUP_WINDOW:
        return ""
    return f"\n(next message, from {nxt.locator.get('sender', 'someone')}): {nxt.text[:500]}"


def build_context(
    conn: sqlite3.Connection, embedder: Embedder, user_id: int, question: str, tz_name: str,
    max_distance: float = 0.5,
) -> tuple[list[Citation], list[Event], dict[int, int]]:
    results = search(conn, embedder, user_id, question, k=TOP_K, max_distance=max_distance)
    blocks: list[Citation] = []
    seg_to_n: dict[int, int] = {}
    for r in results:
        n = len(blocks) + 1
        snippet = r.hit.text
        if r.hit.source_type == "whatsapp":
            snippet += _chat_followup(conn, user_id, r.hit)
        blocks.append(Citation(n=n, chunk_id=r.hit.chunk_id, source_id=r.hit.source_id, segment_id=r.hit.segment_id,
                               source_type=r.hit.source_type, label=describe(r.hit, tz_name),
                               snippet=snippet, observed_at=r.hit.observed_at, locator=r.hit.locator))
        seg_to_n.setdefault(r.hit.segment_id, n)

    events = relevant_events(conn, user_id, question, set(seg_to_n))
    # Events whose source isn't in context yet: add their segment so the event is citable.
    missing = [e.segment_id for e in events if e.segment_id and e.segment_id not in seg_to_n]
    for seg_id in dict.fromkeys(missing):
        chunks = repo.list_chunks(conn, user_id, seg_id)
        if not chunks:
            continue
        hit = repo.get_chunk_hits(conn, user_id, [chunks[0].id])[chunks[0].id]
        n = len(blocks) + 1
        blocks.append(Citation(n=n, chunk_id=hit.chunk_id, source_id=hit.source_id, segment_id=seg_id, source_type=hit.source_type,
                               label=describe(hit, tz_name), snippet=repo.get_segment(conn, user_id, seg_id).text[:1500],
                               observed_at=hit.observed_at, locator=hit.locator))
        seg_to_n[seg_id] = n
    return blocks, events, seg_to_n


def moved_from(conn: sqlite3.Connection, user_id: int, events: list[Event], tz_name: str) -> dict[int, str]:
    """event_id -> 'moved from Wed 21 Oct 2026, 10:00 AM' for events that superseded an earlier date."""
    out = {}
    for event_id, olds in repo.superseded_by_map(conn, user_id, [e.id for e in events]).items():
        dates = [fmt_date(o.due_at, tz_name) if o.all_day else fmt_time(o.due_at, tz_name) for o in olds]
        out[event_id] = "moved from " + " / ".join(dates)
    return out


def render_prompt(question: str, blocks: list[Citation], events: list[Event], seg_to_n: dict[int, int],
                  tz_name: str, now: datetime, moved: dict[int, str] | None = None) -> str:
    lines = [f"Today is {now.astimezone(ZoneInfo(tz_name)).strftime('%A %d %B %Y, %I:%M %p')} ({tz_name}).", "",
             "CONTEXT:"]
    for b in blocks:
        lines += [f"[{b.n}] {b.label}", b.snippet.strip(), ""]
    if events:
        lines.append("KNOWN EVENTS (extracted from the sources above):")
        for e in events:
            flag = " — CONFLICT: another source gives a different date" if e.status == "conflict" else ""
            when = fmt_date(e.due_at, tz_name) + " (no time given)" if e.all_day else fmt_time(e.due_at, tz_name)
            note = f" — UPDATED, {moved[e.id]} (the old date no longer applies)" if moved and e.id in moved else ""
            lines.append(f"- {e.title} ({e.type}{', ' + e.subject if e.subject else ''}): "
                         f"{when} [{seg_to_n.get(e.segment_id, '?')}]{flag}{note}")
        lines.append("")
    lines += ["QUESTION:", question]
    return "\n".join(lines)


_CITE = re.compile(r"\[(\d+)\]")


def ask(
    conn: sqlite3.Connection, embedder: Embedder, llm: LLM | None, user_id: int, question: str,
    *, tz_name: str = "Asia/Kolkata", max_distance: float = 0.5, now: datetime | None = None,
) -> Answer:
    blocks, events, seg_to_n = build_context(conn, embedder, user_id, question, tz_name, max_distance)
    if not blocks:
        return Answer(answer=NOT_FOUND, citations=[], context=[])
    if llm is None:
        lines = ["(No LLM configured, so here are your most relevant saved items.)"]
        lines += [f"[{b.n}] {b.label}: {b.snippet[:200]}" for b in blocks[:5]]
        return Answer(answer="\n".join(lines), citations=blocks[:5], context=blocks)

    prompt = render_prompt(question, blocks, events, seg_to_n, tz_name, now or datetime.now(timezone.utc),
                           moved_from(conn, user_id, events, tz_name))
    text = llm.chat(SYSTEM_PROMPT, prompt).strip()
    valid = {b.n for b in blocks}
    if NOT_FOUND not in text and not any(int(n) in valid for n in _CITE.findall(text)):
        # Every claim must be cited: ask once more before accepting an uncited answer.
        text = llm.chat(SYSTEM_PROMPT, prompt + "\n\nYour previous answer had no [n] citations:\n"
                        f"{text}\nRewrite it, citing the block number for every claim.").strip() or text
    # Drop citation markers that point at nothing.
    text = _CITE.sub(lambda m: m.group(0) if int(m.group(1)) in valid else "", text)
    cited = sorted({int(n) for n in _CITE.findall(text)})
    by_n = {b.n: b for b in blocks}
    return Answer(answer=text or NOT_FOUND, citations=[by_n[n] for n in cited], context=blocks)


def telegram_reply_target(answer: Answer, chat_id: int) -> int | None:
    """message_id of the first cited Telegram source that lives in this chat."""
    for c in answer.citations:
        loc = c.locator
        if c.source_type == "telegram" and loc.get("chat_id") == chat_id and loc.get("message_id"):
            return int(loc["message_id"])
    return None
