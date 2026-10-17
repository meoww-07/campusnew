"""Web API + login link + Gmail OAuth endpoints.

Auth: /login?token=<one-time token from /web> sets an HTTP-only, signed session
cookie. Every /api route derives user_id from that cookie only. Mutating
requests must carry the X-CampusMind header (a cross-site form can't set it),
which together with SameSite=Lax protects against CSRF.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import sqlite3
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app import repo
from app.config import Settings, get_settings
from app.db import connect
from app.ingest import gmail, whatsapp
from app.ingest.upload import UploadRejected, detect_kind, store_file
from app.models import Category, Event, EventType, SourceType, Space, SpaceRules, User
from app.organize import spaces
from app.retrieve import answer as answering
from app.retrieve import today
from app.retrieve.events_view import event_payloads
from app.search.hybrid import SearchFilters, search
from app.web.calendar_ics import build_ics

log = logging.getLogger(__name__)
router = APIRouter()
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "cm_session"
SESSION_TTL = timedelta(days=30)
CSRF_HEADER = "X-CampusMind"


# ---------- sessions ----------

def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(user_id: int, secret: str, ttl: timedelta = SESSION_TTL) -> str:
    payload = f"{user_id}.{int(time.time() + ttl.total_seconds())}"
    raw = f"{payload}.{_sign(payload, secret)}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def read_session(value: str | None, secret: str) -> int | None:
    if not value:
        return None
    try:
        user_id, exp, sig = base64.urlsafe_b64decode(value.encode()).decode().split(".")
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(sig, _sign(f"{user_id}.{exp}", secret)) or int(exp) < time.time():
        return None
    return int(user_id)


# ---------- dependencies ----------

def settings_dep() -> Settings:
    return get_settings()


def db(settings: Settings = Depends(settings_dep)) -> Iterator[sqlite3.Connection]:
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn: sqlite3.Connection = Depends(db),
                 settings: Settings = Depends(settings_dep)) -> User:
    user_id = read_session(request.cookies.get(COOKIE), settings.session_secret)
    user = repo.get_user(conn, user_id) if user_id else None
    if user is None:
        raise HTTPException(401, "Not logged in. Send /web to the bot for a login link.")
    return user


def csrf(x_campusmind: str | None = Header(default=None, alias=CSRF_HEADER)) -> None:
    if x_campusmind != "1":
        raise HTTPException(403, "missing CSRF header")


def _page(title: str, message: str, status: int = 200) -> HTMLResponse:
    body = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><body style='font-family:system-ui;max-width:32rem;margin:3rem auto;"
            f"padding:0 1rem;line-height:1.5'><h2>{html.escape(title)}</h2><p>{html.escape(message)}</p>"
            f"<p><a href='/'>Open CampusMind</a></p></body>")
    return HTMLResponse(body, status_code=status)


# ---------- pages + login ----------

@router.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


def _confirm_page(title: str, action: str, token: str) -> HTMLResponse:
    """GET must not use up a one-time token: link previews (Telegram, WhatsApp, mail
    scanners) open links before the person does. This page posts the token back with
    one automatic step, which preview bots don't perform; the button is a fallback."""
    body = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=robots content=noindex><title>{html.escape(title)}</title>"
            f"<body style='font-family:system-ui;background:#141311;color:#F4F2EC;display:grid;place-items:center;"
            f"min-height:100vh;margin:0'><form method=post action='{html.escape(action)}' style='text-align:center'>"
            f"<input type=hidden name=token value='{html.escape(token)}'><h2>{html.escape(title)}</h2>"
            f"<button style='font:600 16px system-ui;padding:12px 22px;border-radius:10px;border:0;"
            f"background:#D4E77C;color:#17160F;cursor:pointer'>Continue</button></form>"
            f"<script>document.forms[0].submit()</script></body>")
    return HTMLResponse(body, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/login", include_in_schema=False)
def login_page(token: str = Query(...)):
    return _confirm_page("Signing you in to CampusMind…", "/login", token)


@router.post("/login", include_in_schema=False)
def login(token: str = Form(...), conn: sqlite3.Connection = Depends(db),
          settings: Settings = Depends(settings_dep)):
    user_id = repo.consume_login_token(conn, token)
    if user_id is None:
        return _page("Link expired", "This login link was already used or has expired. "
                                     "Send /web to the bot for a new one.", 400)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, make_session(user_id, settings.session_secret), max_age=int(SESSION_TTL.total_seconds()),
                    httponly=True, samesite="lax", secure=settings.app_base_url.startswith("https://"), path="/")
    return resp


@router.post("/api/logout", dependencies=[Depends(csrf)])
def logout():
    resp = HTMLResponse("", status_code=204)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/api/me")
def me(request: Request, user: User = Depends(current_user), settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    tg = getattr(request.app.state, "telegram", None)
    return {"name": user.name, "timezone": user.timezone, "gmail_connected": bool(user.gmail_refresh_token),
            "gmail_available": settings.gmail_configured, "llm_available": settings.llm_configured,
            "gmail_last_sync": user.gmail_last_sync, "last_seen_today_at": user.last_seen_today_at,
            "bot_username": tg.bot.username if tg is not None and tg.bot._bot_user else None}


# ---------- search + ask ----------

@router.get("/api/search")
def api_search(request: Request, q: str = Query(..., min_length=1, max_length=500), k: int = Query(10, ge=1, le=50),
               type: list[SourceType] | None = Query(None), category: Category | None = None,
               start: datetime | None = None, end: datetime | None = None, space_id: int | None = None,
               mode: Literal["hybrid", "keyword"] = "hybrid",
               user: User = Depends(current_user), conn: sqlite3.Connection = Depends(db),
               settings: Settings = Depends(settings_dep)) -> list[dict[str, Any]]:
    """mode=hybrid: words + meaning (RRF). mode=keyword: exact words only (FTS5), fastest."""
    if space_id is not None and repo.get_space(conn, user.id, space_id) is None:
        raise HTTPException(404, "space not found")
    filters = SearchFilters(source_types=type, category=category, start=start, end=end, space_id=space_id)
    results = search(conn, request.app.state.embedder, user.id, q, k=k, filters=filters,
                     max_distance=settings.vector_max_distance if mode == "hybrid" else -1.0)
    seg_spaces = repo.spaces_of_segments(conn, user.id, list({r.hit.segment_id for r in results}))
    return [{"text": r.hit.text, "score": r.score, "locator": r.hit.locator, "observed_at": r.hit.observed_at,
             "segment_id": r.hit.segment_id, "category": r.hit.category,
             "spaces": seg_spaces.get(r.hit.segment_id, []),
             "source": {"id": r.hit.source_id, "type": r.hit.source_type, "title": r.hit.source_title},
             "label": answering.describe(r.hit, user.timezone)} for r in results]


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


@router.post("/api/ask", dependencies=[Depends(csrf)])
def api_ask(body: AskIn, request: Request, user: User = Depends(current_user),
            conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> answering.Answer:
    return answering.ask(conn, request.app.state.embedder, request.app.state.llm, user.id, body.question,
                         tz_name=user.timezone, max_distance=settings.vector_max_distance)


# ---------- events + sources ----------

# ---------- calendar ----------

def _aware(dt: datetime, tz_name: str) -> datetime:
    """Query/body datetimes without an offset are in the user's timezone."""
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz_name))


def _all_day_time(day: datetime, type_: str, tz_name: str) -> datetime:
    """All-day items get the same fixed local time as extracted ones (09:00, deadlines 23:59)."""
    local = _aware(day, tz_name).astimezone(ZoneInfo(tz_name))
    t = dtime(23, 59) if type_ == "deadline" else dtime(9, 0)
    return datetime.combine(local.date(), t, ZoneInfo(tz_name))


@router.get("/api/events")
def api_events(start: datetime | None = None, end: datetime | None = None, days: int = Query(30, ge=1, le=366),
               space_id: int | None = None, type: list[EventType] | None = Query(None),
               user: User = Depends(current_user), conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
    """Events in [start, end] (ISO 8601; no offset = user's timezone). Default: next `days` days."""
    now = datetime.now(timezone.utc)
    start_dt = _aware(start, user.timezone) if start else now - timedelta(hours=12)
    end_dt = _aware(end, user.timezone) if end else start_dt + timedelta(days=days)
    if end_dt < start_dt:
        raise HTTPException(400, "end must be after start")
    if (end_dt - start_dt) > timedelta(days=400):
        raise HTTPException(400, "range too large (max 400 days)")
    space = _space_or_404(conn, user.id, space_id) if space_id is not None else None
    events = repo.list_events_range(conn, user.id, start_dt, end_dt, space_id=space_id, types=type)
    if space is not None:  # a subject space shows only its own subject's events
        events = [e for e in events if spaces._event_belongs(space, e.model_dump())]
    return event_payloads(conn, user.id, events, now)


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    type: EventType = "event"
    start: datetime
    end: datetime | None = None
    all_day: bool = False
    location: str | None = Field(default=None, max_length=200)
    subject: str | None = Field(default=None, max_length=80)


class EventPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    type: EventType | None = None
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool | None = None
    location: str | None = Field(default=None, max_length=200)
    subject: str | None = Field(default=None, max_length=80)
    status: Literal["upcoming", "done", "cancelled"] | None = None


class ResolveIn(BaseModel):
    action: Literal["update", "keep_both"]


def _event_or_404(conn: sqlite3.Connection, user_id: int, event_id: int) -> Event:
    e = repo.get_event(conn, user_id, event_id)
    if e is None:
        raise HTTPException(404, "event not found")
    return e


@router.post("/api/events", dependencies=[Depends(csrf)], status_code=201)
def api_create_event(body: EventCreate, user: User = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
    start = _all_day_time(body.start, body.type, user.timezone) if body.all_day else _aware(body.start, user.timezone)
    end = _aware(body.end, user.timezone) if body.end else None
    try:
        e = repo.create_event(conn, user.id, title=body.title, type=body.type, due_at=start, end_at=end,
                              all_day=body.all_day, location=body.location, subject=body.subject)
    except ValueError as err:
        raise HTTPException(400, str(err)) from err
    return event_payloads(conn, user.id, [e])[0]


@router.get("/api/events/{event_id}")
def api_event(event_id: int, user: User = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
    e = _event_or_404(conn, user.id, event_id)
    payload = event_payloads(conn, user.id, [e])[0]
    payload["conflicts_with"] = event_payloads(conn, user.id, repo.conflict_partners(conn, user.id, event_id))
    return payload


@router.patch("/api/events/{event_id}", dependencies=[Depends(csrf)])
def api_update_event(event_id: int, body: EventPatch, user: User = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
    current = _event_or_404(conn, user.id, event_id)
    fields: dict[str, Any] = body.model_dump(exclude_unset=True, exclude={"start", "end"})
    all_day = body.all_day if body.all_day is not None else current.all_day
    type_ = body.type or current.type
    if body.start is not None:
        fields["due_at"] = _all_day_time(body.start, type_, user.timezone) if all_day else _aware(body.start, user.timezone)
    if "end" in body.model_fields_set:
        fields["end_at"] = _aware(body.end, user.timezone) if body.end else None
    try:
        e = repo.update_event(conn, user.id, event_id, **fields)
    except ValueError as err:
        raise HTTPException(400, str(err)) from err
    return event_payloads(conn, user.id, [e])[0]


@router.delete("/api/events/{event_id}", dependencies=[Depends(csrf)])
def api_delete_event(event_id: int, user: User = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)) -> dict[str, bool]:
    if not repo.delete_event(conn, user.id, event_id):
        raise HTTPException(404, "event not found")
    return {"deleted": True}


@router.post("/api/events/{event_id}/resolve", dependencies=[Depends(csrf)])
def api_resolve_conflict(event_id: int, body: ResolveIn, user: User = Depends(current_user),
                         conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
    """Conflict buttons: 'This is an update' (this date wins) / 'Keep both'."""
    _event_or_404(conn, user.id, event_id)
    return event_payloads(conn, user.id, repo.resolve_conflict(conn, user.id, event_id, body.action))


@router.post("/api/events/{event_id}/restore", dependencies=[Depends(csrf)])
def api_restore_event(event_id: int, user: User = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
    """Undo an automatic 'moved to' update: both dates become a conflict again."""
    _event_or_404(conn, user.id, event_id)
    if not repo.undo_supersede(conn, user.id, event_id):
        raise HTTPException(400, "this event was not replaced by an update")
    return event_payloads(conn, user.id, [repo.get_event(conn, user.id, event_id)])[0]


@router.get("/api/calendar/subscription")
def api_calendar_subscription(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(db),
                              settings: Settings = Depends(settings_dep)) -> dict[str, str]:
    token = repo.get_or_create_ical_token(conn, user.id)
    url = f"{settings.app_base_url}/api/calendar.ics?token={token}"
    return {"url": url, "webcal": url.replace("https://", "webcal://").replace("http://", "webcal://")}


@router.post("/api/calendar/subscription/regenerate", dependencies=[Depends(csrf)])
def api_regenerate_subscription(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(db),
                                settings: Settings = Depends(settings_dep)) -> dict[str, str]:
    repo.regenerate_ical_token(conn, user.id)
    return api_calendar_subscription(user, conn, settings)


@router.get("/api/calendar.ics", include_in_schema=False)
def api_calendar_ics(token: str = Query(..., min_length=10), conn: sqlite3.Connection = Depends(db),
                     settings: Settings = Depends(settings_dep)) -> Response:
    """Read-only feed for Google Calendar 'From URL'. Authenticated by the per-user secret token."""
    user = repo.user_by_ical_token(conn, token)
    if user is None:
        raise HTTPException(404, "unknown calendar")
    now = datetime.now(timezone.utc)
    events = repo.list_events_range(conn, user.id, now - timedelta(days=60), now + timedelta(days=365))
    body = build_ics(events, user.timezone, f"CampusMind ({user.name})" if user.name else "CampusMind",
                     settings.app_base_url, now)
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'inline; filename="campusmind.ics"', "Cache-Control": "no-cache"})


@router.get("/api/sources")
def api_sources(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
    return repo.list_sources_overview(conn, user.id)


@router.delete("/api/sources/{source_id}", dependencies=[Depends(csrf)])
def api_delete_source(source_id: int, user: User = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)) -> dict[str, bool]:
    if not repo.delete_source(conn, user.id, source_id):
        raise HTTPException(404, "source not found")
    return {"deleted": True}


@router.post("/api/upload", dependencies=[Depends(csrf)])
async def api_upload(file: UploadFile = File(...), user: User = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, f"File is too large (max {settings.max_upload_bytes // (1024 * 1024)} MB).")
    kind = detect_kind(data)
    if kind == "txt":
        res = whatsapp.import_export(conn, user.id, data, file.filename)
        if res is None:
            raise HTTPException(422, "That text file doesn't look like a WhatsApp chat export.")
        return {"source_id": res.source_id, "created": res.created, "kind": "whatsapp"}
    try:
        res = store_file(conn, settings.files_dir, user.id, data, source_type="upload", filename=file.filename,
                         observed_at=datetime.now(timezone.utc), max_bytes=settings.max_upload_bytes)
    except UploadRejected as e:
        raise HTTPException(415, str(e)) from e
    return {"source_id": res.source_id, "created": res.created, "kind": kind}


# ---------- Today ----------

@router.get("/api/today")
def api_today(mark_seen: bool = False, user: User = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
    """The home feed. `mark_seen=true` (first load of a visit) moves the "new since" marker
    forward *after* building, so auto-refreshes during the visit keep showing the same new items."""
    now = datetime.now(timezone.utc)
    feed = today.build_today(conn, user, now)
    if mark_seen:
        repo.set_last_seen_today(conn, user.id, now)
    return feed


class SnoozeIn(BaseModel):
    kind: Literal["event", "segment"]
    id: int


@router.post("/api/today/snooze", dependencies=[Depends(csrf)])
def api_snooze(body: SnoozeIn, user: User = Depends(current_user),
               conn: sqlite3.Connection = Depends(db)) -> dict[str, str]:
    until = today.snooze_until(user)
    if not repo.snooze(conn, user.id, body.kind, body.id, until):
        raise HTTPException(404, "item not found")
    return {"until": repo.iso(until)}


@router.get("/api/segments/{segment_id}")
def api_segment(segment_id: int, user: User = Depends(current_user),
                conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
    """Full text of one saved item, with its source, spaces and events ("Open original")."""
    brief = repo.segment_brief(conn, user.id, [segment_id]).get(segment_id)
    if brief is None:
        raise HTTPException(404, "item not found")
    seg = repo.get_segment(conn, user.id, segment_id)
    events = [e for e in repo.list_events(conn, user.id, statuses=repo.CALENDAR_STATUSES) if e.segment_id == segment_id]
    return {**brief, "summary": seg.summary, "subject": seg.subject, "opened_at": seg.opened_at,
            "spaces": repo.spaces_of_segments(conn, user.id, [segment_id]).get(segment_id, []),
            "events": event_payloads(conn, user.id, events),
            "label": answering.describe_segment(brief, user.timezone)}


@router.post("/api/segments/{segment_id}/opened", dependencies=[Depends(csrf)])
def api_segment_opened(segment_id: int, user: User = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db)) -> dict[str, bool]:
    if not repo.mark_segment_opened(conn, user.id, segment_id):
        raise HTTPException(404, "item not found")
    return {"opened": True}


# ---------- Smart Spaces ----------

class SpaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    icon: str | None = Field(default=None, max_length=8)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    rules: SpaceRules = Field(default_factory=SpaceRules)
    pinned: bool = False


class SpacePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    icon: str | None = Field(default=None, max_length=8)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    rules: SpaceRules | None = None
    pinned: bool | None = None


class MergeIn(BaseModel):
    into_id: int


class ItemIn(BaseModel):
    segment_id: int


def _space_or_404(conn: sqlite3.Connection, user_id: int, space_id: int) -> Space:
    space = repo.get_space(conn, user_id, space_id)
    if space is None:
        raise HTTPException(404, "space not found")
    return space


@router.get("/api/spaces")
def api_spaces(user: User = Depends(current_user), conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
    if not repo.auto_key_exists(conn, user.id, spaces.CATEGORY_SPACES[0].key):
        # First visit for a user whose data predates Smart Spaces: organize it once.
        repo.enqueue_job(conn, user.id, "spaces_backfill")
    spaces.ensure_category_spaces(conn, user.id)
    return spaces.list_overview(conn, user.id)


@router.post("/api/spaces/preview", dependencies=[Depends(csrf)])
def api_space_preview(rules: SpaceRules, request: Request, user: User = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
    """Live preview for the rule builder: '12 items match'."""
    return spaces.preview_rules(conn, user.id, rules, request.app.state.embedder)


@router.post("/api/spaces", dependencies=[Depends(csrf)], status_code=201)
def api_create_space(body: SpaceIn, user: User = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)) -> Space:
    try:
        space = repo.create_space(conn, user.id, body.name, "custom", icon=body.icon, color=body.color,
                                  rules=body.rules, pinned=body.pinned)
    except repo.SpaceNameTaken as e:
        raise HTTPException(409, str(e)) from e
    if not space.rules.is_empty():
        repo.enqueue_job(conn, user.id, "space_backfill", space_id=space.id)  # worker fills it
    return space


@router.patch("/api/spaces/{space_id}", dependencies=[Depends(csrf)])
def api_update_space(space_id: int, body: SpacePatch, user: User = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)) -> Space:
    _space_or_404(conn, user.id, space_id)
    try:
        space = repo.update_space(conn, user.id, space_id, name=body.name, icon=body.icon, color=body.color,
                                  pinned=body.pinned, rules=body.rules)
    except repo.SpaceNameTaken as e:
        raise HTTPException(409, str(e)) from e
    if body.rules is not None:
        repo.enqueue_job(conn, user.id, "space_backfill", space_id=space_id)
    return space


@router.post("/api/spaces/{space_id}/merge", dependencies=[Depends(csrf)])
def api_merge_spaces(space_id: int, body: MergeIn, user: User = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)) -> Space:
    try:
        target = repo.merge_spaces(conn, user.id, space_id, body.into_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if target is None:
        raise HTTPException(404, "space not found")
    return target


@router.delete("/api/spaces/{space_id}", dependencies=[Depends(csrf)])
def api_delete_space(space_id: int, user: User = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)) -> dict[str, bool]:
    if not repo.delete_space(conn, user.id, space_id):
        raise HTTPException(404, "space not found")
    return {"deleted": True}


@router.get("/api/spaces/{space_id}/items")
def api_space_items(space_id: int, cursor: str | None = None, limit: int = Query(20, ge=1, le=100),
                    type: Category | None = None, source: SourceType | None = None,
                    user: User = Depends(current_user), conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
    _space_or_404(conn, user.id, space_id)
    try:
        items, next_cursor = repo.list_space_items(conn, user.id, space_id, cursor=cursor, limit=limit,
                                                   category=type, source_type=source)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    in_spaces = repo.spaces_of_segments(conn, user.id, [i["segment_id"] for i in items])
    for item in items:
        item["spaces"] = in_spaces.get(item["segment_id"], [])
    return {"items": items, "next_cursor": next_cursor}


@router.post("/api/spaces/{space_id}/items", dependencies=[Depends(csrf)], status_code=201)
def api_add_space_item(space_id: int, body: ItemIn, user: User = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db)) -> dict[str, bool]:
    _space_or_404(conn, user.id, space_id)
    if repo.get_segment(conn, user.id, body.segment_id) is None:
        raise HTTPException(404, "item not found")
    repo.add_space_items(conn, user.id, space_id, [(body.segment_id, "user", 1.0)])
    return {"added": True}


@router.delete("/api/spaces/{space_id}/items/{segment_id}", dependencies=[Depends(csrf)])
def api_remove_space_item(space_id: int, segment_id: int, user: User = Depends(current_user),
                          conn: sqlite3.Connection = Depends(db)) -> dict[str, bool]:
    if not repo.remove_space_item(conn, user.id, space_id, segment_id):
        raise HTTPException(404, "space or item not found")
    return {"removed": True}


# ---------- Gmail OAuth ----------

@router.get("/gmail/connect", include_in_schema=False)
def gmail_connect_page(request: Request, token: str | None = None, conn: sqlite3.Connection = Depends(db),
                       settings: Settings = Depends(settings_dep)):
    if token:  # from the bot: confirm first so link previews can't use up the token
        return _confirm_page("Opening Google sign-in…", "/gmail/connect", token)
    return gmail_connect(request, None, conn, settings)  # from the web page (session cookie)


@router.post("/gmail/connect", include_in_schema=False)
def gmail_connect(request: Request, token: str | None = Form(None), conn: sqlite3.Connection = Depends(db),
                  settings: Settings = Depends(settings_dep)):
    user_id = repo.consume_login_token(conn, token) if token else \
        read_session(request.cookies.get(COOKIE), settings.session_secret)
    if user_id is None:
        return _page("Link expired", "Send /gmail to the bot for a new Gmail connect link.", 400)
    try:
        return RedirectResponse(gmail.start_connect(settings, user_id), status_code=303)
    except gmail.OAuthError as e:
        return _page("Gmail unavailable", str(e), 503)


@router.get("/gmail/callback", include_in_schema=False)
def gmail_callback(background: BackgroundTasks, state: str = "", code: str = "", error: str = "",
                   conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)):
    if error or not code:
        return _page("Gmail not connected", f"Google returned: {error or 'no authorization code'}.", 400)
    try:
        user_id = gmail.finish_connect(conn, settings, state, code)
    except gmail.OAuthError as e:
        return _page("Gmail not connected", str(e), 400)
    except Exception:  # noqa: BLE001 - token exchange failure
        log.exception("gmail token exchange failed")
        return _page("Gmail not connected", "Couldn't complete the Google sign-in. Please try again.", 400)
    background.add_task(gmail.sync_one, settings, user_id)
    return _page("Gmail connected ✓", "CampusMind will now read your recent emails (read-only) and keep them "
                                     "in sync every 15 minutes. You can close this tab.")
