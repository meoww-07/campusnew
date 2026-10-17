"""Job loop: claim -> extract text -> chunk -> index -> LLM extraction -> done.

Job kinds:
  process  segments were stored at ingest (Telegram text, WhatsApp); index them
  pdf      extract per page (OCR fallback), one segment per page
  image    OCR the image (+ caption), one segment
  gmail    email body segment exists; attachments (PDF/images) are extracted

After indexing, each segment goes through LLM extraction (summary, category,
importance, events) when an LLM is configured.

Every step is idempotent so a retried job doesn't duplicate data: file
segments are only extracted if the source has none, attachments only if not
seen yet, only segments without chunks get indexed, and only segments without
extracted_at go to the LLM.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta

from app import repo
from app.ingest.upload import detect_kind
from app.llm import LLM
from app.models import Job, Segment, SegmentIn
from app.organize import extract_text, llm_extract, spaces
from app.organize.indexer import Embedder, index_segments

log = logging.getLogger(__name__)


class JobError(RuntimeError):
    pass


def _extract_file_segments(job: Job, file_path: str) -> list[SegmentIn]:
    payload = job.payload
    observed_at = datetime.fromisoformat(payload["observed_at"])
    base_locator = payload.get("locator") or {}
    caption = (payload.get("caption") or "").strip()

    if job.kind == "pdf":
        pages = extract_text.extract_pdf(file_path)
        segs = [
            SegmentIn(text=p.text, observed_at=observed_at,
                      locator={**base_locator, "page": p.page, "ocr": p.ocr})
            for p in pages if p.text
        ]
        if caption:
            segs.insert(0, SegmentIn(text=caption, observed_at=observed_at,
                                     locator={**base_locator, "caption": True}))
        return segs

    if job.kind == "image":
        text = extract_text.ocr_image_file(file_path)
        full = (f"Caption: {caption}\n\n" if caption else "") + text if text else caption
        return [SegmentIn(text=full, observed_at=observed_at, locator={**base_locator, "ocr": True})] if full else []

    raise JobError(f"unknown job kind {job.kind!r}")


def _extract_attachments(conn: sqlite3.Connection, job: Job) -> None:
    """Gmail: segments per attachment, skipping attachments already extracted."""
    done = {s.locator.get("attachment") for s in repo.list_segments(conn, job.user_id, job.source_id)}
    observed_at = datetime.fromisoformat(job.payload["observed_at"])
    base = job.payload.get("locator") or {}
    for att in job.payload.get("attachments", []):
        if att["filename"] in done:
            continue
        with open(att["path"], "rb") as f:
            kind = detect_kind(f.read(16))
        loc = {**base, "attachment": att["filename"]}
        if kind == "pdf":
            segs = [SegmentIn(text=p.text, observed_at=observed_at, locator={**loc, "page": p.page, "ocr": p.ocr})
                    for p in extract_text.extract_pdf(att["path"]) if p.text]
        else:
            text = extract_text.ocr_image_file(att["path"])
            segs = [SegmentIn(text=text, observed_at=observed_at, locator={**loc, "ocr": True})] if text else []
        if not segs:  # marker so an empty attachment isn't retried forever
            segs = [SegmentIn(text=f"[attachment {att['filename']}: no text]", observed_at=observed_at, locator=loc)]
        repo.add_segments(conn, job.user_id, job.source_id, segs)


CHAT_CONTEXT_WINDOW = timedelta(minutes=30)


def chat_context(segments: list[Segment]) -> dict[int, str]:
    """For chat messages: the previous message, if sent within the window.

    Replies like "Yes, up to BCNF" only make sense next to their question, so
    the question is embedded together with the reply.
    """
    out: dict[int, str] = {}
    for prev, cur in zip(segments, segments[1:]):
        gap = datetime.fromisoformat(cur.observed_at) - datetime.fromisoformat(prev.observed_at)
        if timedelta(0) <= gap <= CHAT_CONTEXT_WINDOW:
            out[cur.id] = prev.text[:500]
    return out


SUMMARY_MIN_CHARS = 300  # longer items also get their LLM summary indexed for search


def source_hint(seg: Segment) -> str | None:
    loc = seg.locator
    if loc.get("ocr"):
        return "photo or scanned page; the text was read by OCR and may contain recognition errors"
    if loc.get("page"):
        return f"page {loc['page']} of a PDF document"
    if loc.get("gmail_id"):
        return "email"
    return None


def _extract_llm(conn: sqlite3.Connection, user_id: int, segments: list[Segment], llm: LLM | None,
                 tz_name: str, embedder: Embedder | None = None) -> int:
    """LLM extraction per segment. Returns events stored.

    A failing segment doesn't stop the others; if any failed, JobError is
    raised at the end so the job is retried (finished segments are skipped).
    """
    n_events = 0
    errors: list[str] = []
    for seg in segments:
        observed_at = datetime.fromisoformat(seg.observed_at)
        if len(seg.text.strip()) < llm_extract.MIN_SEGMENT_CHARS:
            repo.apply_extraction(conn, user_id, seg.id, summary=None, category="general",
                                  subject=None, importance="low")
            continue
        if llm is None:
            continue  # stays unextracted until a run with an LLM configured
        try:
            ex = llm_extract.extract(llm, seg.text, observed_at, tz_name, source_hint(seg))
        except Exception as e:  # noqa: BLE001 - LLMError, timeouts, connection errors
            errors.append(f"segment {seg.id}: {type(e).__name__}: {str(e)[:200]}")
            continue
        events = llm_extract.to_events(ex, seg.id, observed_at, tz_name)
        n_events += len(repo.apply_extraction(conn, user_id, seg.id, summary=ex.summary, category=ex.category,
                                              subject=ex.subject, importance=ex.importance, events=events))
        if embedder is not None and ex.summary and len(seg.text) >= SUMMARY_MIN_CHARS:
            repo.add_summary_chunk(conn, user_id, seg.id, ex.summary, embedder.embed([ex.summary])[0])
    if errors:
        raise JobError(f"LLM extraction failed for {len(errors)} segment(s); first: {errors[0]}")
    return n_events


def process_job(conn: sqlite3.Connection, job: Job, embedder: Embedder, llm: LLM | None = None) -> int:
    """Run one claimed job. Returns the number of chunks indexed (items added for space jobs)."""
    if job.kind == "space_backfill":  # a custom space was created or its rules changed
        return spaces.backfill_space(conn, job.user_id, job.space_id, embedder) if job.space_id else 0
    if job.kind == "spaces_backfill":  # one-time, queued by migration v4
        return spaces.backfill_user(conn, job.user_id, embedder)
    if job.kind == "reindex":  # rebuild the search index after chunking changes
        return reindex_user(conn, job.user_id, embedder)
    if job.kind == "resolve_conflicts":  # one-time, queued by migration v5
        return repo.resolve_conflicts(conn, job.user_id)
    source = repo.get_source(conn, job.user_id, job.source_id)
    if source is None:
        raise JobError("source no longer exists")
    user = repo.get_user(conn, job.user_id)

    if job.kind in ("pdf", "image") and not repo.list_segments(conn, job.user_id, source.id):
        if not source.file_path:
            raise JobError("file job without file_path")
        segs = _extract_file_segments(job, source.file_path)
        if not segs:
            raise JobError("no text could be extracted")
        repo.add_segments(conn, job.user_id, source.id, segs)
    elif job.kind == "gmail":
        _extract_attachments(conn, job)

    pending = repo.list_unindexed_segments(conn, job.user_id, source.id)
    context = chat_context(repo.list_segments(conn, job.user_id, source.id)) if source.type == "whatsapp" else None
    n = index_segments(conn, job.user_id, pending, embedder, context)
    try:
        _extract_llm(conn, job.user_id, repo.list_unextracted_segments(conn, job.user_id, source.id),
                     llm, user.timezone if user else "Asia/Kolkata", embedder)
    finally:
        # Even if some segments failed extraction, the finished ones get their spaces now.
        spaces.assign_segments(conn, job.user_id, [s.id for s in repo.list_segments(conn, job.user_id, source.id)],
                               embedder)
    return n


def run_once(conn: sqlite3.Connection, embedder: Embedder, llm: LLM | None = None) -> bool:
    """Claim and process one job. Returns False when the queue is empty."""
    job = repo.claim_job(conn)
    if job is None:
        return False
    try:
        n = process_job(conn, job, embedder, llm)
    except Exception as e:  # noqa: BLE001 - every failure is recorded on the job
        status = repo.fail_job(conn, job.id, f"{type(e).__name__}: {e}")
        log.warning("job %s (%s) failed attempt %s -> %s: %s", job.id, job.kind, job.attempts, status, e)
    else:
        repo.complete_job(conn, job.id)
        log.info("job %s (%s) done (%d %s)", job.id, job.kind, n, "items" if job.source_id is None else "chunks")
    return True


def drain(conn: sqlite3.Connection, embedder: Embedder, llm: LLM | None = None) -> int:
    """Process jobs until the queue is empty (seed/eval scripts). Returns jobs run."""
    count = 0
    while run_once(conn, embedder, llm):
        count += 1
    return count


def run_forever(conn: sqlite3.Connection, embedder: Embedder, stop: threading.Event,
                llm: LLM | None = None, idle_sleep: float = 2.0) -> None:
    log.info("worker started (LLM extraction %s)", "on" if llm else "off")
    while not stop.is_set():
        try:
            busy = run_once(conn, embedder, llm)
        except Exception:  # noqa: BLE001 - e.g. DB locked; keep the loop alive
            log.exception("worker loop error")
            busy = False
        if not busy:
            stop.wait(idle_sleep)
    log.info("worker stopped")


def reindex_user(conn: sqlite3.Connection, user_id: int, embedder: Embedder) -> int:
    """Re-chunk and re-embed every saved item of a user (keeps segments, events, spaces).
    Also indexes LLM summaries of long items. Returns chunks written."""
    repo.clear_user_index(conn, user_id)
    total = 0
    for source in repo.list_sources(conn, user_id, limit=100000):
        segs = repo.list_segments(conn, user_id, source.id)
        context = chat_context(segs) if source.type == "whatsapp" else None
        total += index_segments(conn, user_id, segs, embedder, context)
        long_ones = [s for s in segs if s.summary and len(s.text) >= SUMMARY_MIN_CHARS]
        vectors = embedder.embed([s.summary for s in long_ones]) if long_ones else []
        for seg, vec in zip(long_ones, vectors):
            total += repo.add_summary_chunk(conn, user_id, seg.id, seg.summary, vec) is not None
    return total
