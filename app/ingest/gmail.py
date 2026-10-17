"""Gmail: OAuth web flow (gmail.readonly), MIME parsing, sync with attachments.

Flow:  GET /gmail/connect?token=...  -> Google consent -> GET /gmail/callback
       stores the refresh token for that user and triggers a first sync.
Sync:  GMAIL_QUERY (default newer_than:60d) plus "after:<last_sync>" on later
       runs; messages already stored (external_id = gmail id) are skipped.
Each email body is one segment; PDF/image attachments are saved in a folder
owned by the email's source and extracted by the worker (job kind "gmail").
"""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from bs4 import BeautifulSoup

from app import repo
from app.config import Settings
from app.ingest.upload import detect_kind
from app.models import IngestResult, SegmentIn
from app.organize.extract_text import clean

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
MAX_MESSAGES_PER_SYNC = 200
STATE_TTL = 600  # seconds
ATTACHMENT_KINDS = {"pdf", "png", "jpg"}


# ---------- OAuth ----------

class OAuthError(RuntimeError):
    pass


@dataclass
class _Pending:
    user_id: int
    code_verifier: str | None
    expires: float


class OAuthStateStore:
    """Short-lived, single-use OAuth `state` values (CSRF protection + PKCE verifier)."""

    def __init__(self) -> None:
        self._items: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def put(self, state: str, user_id: int, code_verifier: str | None) -> None:
        with self._lock:
            now = time.time()
            self._items = {k: v for k, v in self._items.items() if v.expires > now}
            self._items[state] = _Pending(user_id, code_verifier, now + STATE_TTL)

    def pop(self, state: str) -> _Pending | None:
        with self._lock:
            item = self._items.pop(state, None)
        return item if item and item.expires > time.time() else None


STATES = OAuthStateStore()


def redirect_uri(settings: Settings) -> str:
    return f"{settings.app_base_url}/gmail/callback"


def _flow(settings: Settings, state: str | None = None):
    from google_auth_oauthlib.flow import Flow
    if settings.app_base_url.startswith("http://"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # local dev only
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    config = {"web": {
        "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
        "auth_uri": AUTH_URI, "token_uri": TOKEN_URI, "redirect_uris": [redirect_uri(settings)],
    }}
    return Flow.from_client_config(config, scopes=SCOPES, state=state, redirect_uri=redirect_uri(settings))


def start_connect(settings: Settings, user_id: int, store: OAuthStateStore = STATES) -> str:
    """Return the Google consent URL for this user."""
    if not settings.gmail_configured:
        raise OAuthError("Gmail is not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
    state = secrets.token_urlsafe(24)
    flow = _flow(settings, state)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    store.put(state, user_id, getattr(flow, "code_verifier", None))
    return url


def finish_connect(conn: sqlite3.Connection, settings: Settings, state: str, code: str,
                   store: OAuthStateStore = STATES) -> int:
    """Exchange the code, store the refresh token. Returns the user id."""
    pending = store.pop(state)
    if pending is None:
        raise OAuthError("This Gmail connect link expired. Send /gmail again.")
    flow = _flow(settings, state)
    if pending.code_verifier:
        flow.code_verifier = pending.code_verifier
    flow.fetch_token(code=code)
    refresh = flow.credentials.refresh_token
    if not refresh:
        raise OAuthError("Google didn't return a refresh token. Remove CampusMind at "
                         "myaccount.google.com/permissions and connect again.")
    repo.set_gmail_token(conn, pending.user_id, refresh)
    return pending.user_id


# ---------- API client ----------

class GmailClient(Protocol):
    def list_ids(self, query: str, limit: int) -> Iterable[str]: ...
    def get(self, msg_id: str) -> dict[str, Any]: ...
    def attachment(self, msg_id: str, attachment_id: str) -> bytes: ...


class GoogleGmailClient:
    def __init__(self, settings: Settings, refresh_token: str) -> None:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials(None, refresh_token=refresh_token, token_uri=TOKEN_URI, scopes=SCOPES,
                            client_id=settings.google_client_id, client_secret=settings.google_client_secret)
        self.api = build("gmail", "v1", credentials=creds, cache_discovery=False).users()

    def list_ids(self, query: str, limit: int) -> Iterable[str]:
        token, n = None, 0
        while n < limit:
            resp = self.api.messages().list(userId="me", q=query, pageToken=token,
                                            maxResults=min(100, limit - n)).execute()
            for m in resp.get("messages", []):
                n += 1
                yield m["id"]
            token = resp.get("nextPageToken")
            if not token:
                return

    def get(self, msg_id: str) -> dict[str, Any]:
        return self.api.messages().get(userId="me", id=msg_id, format="full").execute()

    def attachment(self, msg_id: str, attachment_id: str) -> bytes:
        resp = self.api.messages().attachments().get(userId="me", messageId=msg_id, id=attachment_id).execute()
        return b64url(resp["data"])


# ---------- MIME parsing ----------

@dataclass
class Attachment:
    filename: str
    mime_type: str
    size: int
    data: bytes | None = None
    attachment_id: str | None = None


@dataclass
class ParsedEmail:
    gmail_id: str
    thread_id: str
    subject: str
    sender: str
    date: datetime
    body: str
    attachments: list[Attachment] = field(default_factory=list)


def b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_text(data: str | None) -> str:
    return b64url(data).decode("utf-8", errors="replace") if data else ""


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "blockquote"]):
        tag.decompose()
    for tag in soup.select(".gmail_quote, .gmail_signature, #divRplyFwdMsg"):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return soup.get_text("\n")


_QUOTE_HEADERS = [
    re.compile(r"^On\s.{0,300}?wrote:\s*$", re.MULTILINE | re.DOTALL),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^_{10,}\s*\nFrom:", re.MULTILINE),
    re.compile(r"^From:.*\n(Sent|Date):.*\n", re.MULTILINE),
]
_SIGNATURE = [
    re.compile(r"^--\s*$", re.MULTILINE),
    re.compile(r"^Sent from my (iPhone|iPad|Android|Samsung|mobile|phone).*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^Get Outlook for (iOS|Android).*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^(DISCLAIMER|CONFIDENTIALITY NOTICE|IMPORTANT NOTICE)\b", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^(This|The information (contained )?in this) (e-?mail|message|communication)\b.{0,80}"
               r"(confidential|intended (solely )?for|privileged)", re.MULTILINE | re.IGNORECASE),
]


def clean_body(text: str) -> str:
    """Strip quoted replies, signatures and disclaimers."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for pattern in _QUOTE_HEADERS:
        m = pattern.search(text)
        if m and m.start() > 0:
            text = text[:m.start()]
    text = "\n".join(line for line in text.split("\n") if not line.lstrip().startswith(">"))
    for pattern in _SIGNATURE:
        m = pattern.search(text)
        if m and m.start() > 0:
            text = text[:m.start()]
    return clean(text)


def parse_message(msg: dict[str, Any]) -> ParsedEmail:
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plains: list[str] = []
    htmls: list[str] = []
    attachments: list[Attachment] = []

    def walk(part: dict[str, Any]) -> None:
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body", {}) or {}
        filename = part.get("filename") or ""
        if filename and (body.get("attachmentId") or body.get("data")):
            attachments.append(Attachment(
                filename=filename, mime_type=mime, size=int(body.get("size") or 0),
                data=b64url(body["data"]) if body.get("data") else None,
                attachment_id=body.get("attachmentId"),
            ))
        elif mime == "text/plain":
            plains.append(_decode_text(body.get("data")))
        elif mime == "text/html":
            htmls.append(_decode_text(body.get("data")))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    raw = "\n".join(plains) if any(p.strip() for p in plains) else html_to_text("\n".join(htmls))
    ts = int(msg.get("internalDate") or 0) / 1000
    return ParsedEmail(
        gmail_id=msg["id"], thread_id=msg.get("threadId", ""),
        subject=headers.get("subject", "(no subject)").strip(), sender=headers.get("from", "").strip(),
        date=datetime.fromtimestamp(ts, tz=timezone.utc), body=clean_body(raw), attachments=attachments,
    )


# ---------- storing + sync ----------

def _safe_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[^\w.\- ]", "_", Path(name).name).strip() or "attachment"
    base = base[:100]
    candidate, i = base, 1
    while candidate.lower() in used:
        candidate = f"{i}_{base}"
        i += 1
    used.add(candidate.lower())
    return candidate


def store_email(conn: sqlite3.Connection, settings: Settings, user_id: int, email: ParsedEmail,
                client: GmailClient) -> IngestResult | None:
    """Store one email (+ PDF/image attachments) as a gmail source. None if empty."""
    folder = settings.files_dir / str(user_id) / f"gmail_{email.gmail_id}"
    saved: list[dict[str, str]] = []
    digests: list[str] = []
    used: set[str] = set()
    try:
        for att in email.attachments:
            if att.size > settings.max_upload_bytes:
                continue
            data = att.data if att.data is not None else client.attachment(email.gmail_id, att.attachment_id or "")
            if len(data) > settings.max_upload_bytes or detect_kind(data) not in ATTACHMENT_KINDS:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            name = _safe_name(att.filename, used)
            (folder / name).write_bytes(data)
            saved.append({"filename": name, "path": str(folder / name)})
            digests.append(repo.content_hash(data))

        if not email.body and not saved:
            return None
        locator = {"gmail_id": email.gmail_id, "thread_id": email.thread_id,
                   "from": email.sender, "subject": email.subject}
        text = f"Subject: {email.subject}\nFrom: {email.sender}\n\n{email.body}".strip()
        result = repo.ingest_source(
            conn, user_id, "gmail", repo.content_hash("gmail\n" + text + "\n".join(digests)),
            [SegmentIn(text=text, observed_at=email.date, locator=locator)],
            external_id=email.gmail_id, title=email.subject[:200],
            file_path=str(folder) if saved else None, job_kind="gmail",
            job_payload={"observed_at": repo.iso(email.date), "locator": locator, "attachments": saved},
        )
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    if not result.created and saved:
        shutil.rmtree(folder, ignore_errors=True)  # duplicate content: nothing references these files
    return result


@dataclass
class SyncResult:
    listed: int = 0
    stored: int = 0
    skipped: int = 0


def build_query(base: str, last_sync: str | None) -> str:
    if not last_sync:
        return base
    after = int(datetime.fromisoformat(last_sync).timestamp()) - 300  # small overlap; dedup handles it
    return f"{base} after:{after}"


def sync_user(conn: sqlite3.Connection, settings: Settings, user_id: int, client: GmailClient,
              now: datetime | None = None) -> SyncResult:
    started = now or datetime.now(timezone.utc)
    user = repo.get_user(conn, user_id)
    if user is None:
        raise ValueError("unknown user")
    res = SyncResult()
    for msg_id in client.list_ids(build_query(settings.gmail_query, user.gmail_last_sync), MAX_MESSAGES_PER_SYNC):
        res.listed += 1
        if repo.external_id_exists(conn, user_id, "gmail", msg_id):
            res.skipped += 1
            continue
        stored = store_email(conn, settings, user_id, parse_message(client.get(msg_id)), client)
        if stored and stored.created:
            res.stored += 1
        else:
            res.skipped += 1
    repo.set_gmail_last_sync(conn, user_id, started)
    return res


def _sync_safely(conn: sqlite3.Connection, settings: Settings, user_id: int) -> SyncResult | str:
    from google.auth.exceptions import RefreshError
    user = repo.get_user(conn, user_id)
    if user is None or not user.gmail_refresh_token:
        return "not connected"
    try:
        result = sync_user(conn, settings, user_id, GoogleGmailClient(settings, user.gmail_refresh_token))
        log.info("gmail sync user %s: %s", user_id, result)
        return result
    except RefreshError as e:
        log.warning("gmail token revoked for user %s: %s", user_id, e)
        repo.set_gmail_token(conn, user_id, None)
        return "revoked"
    except Exception as e:  # noqa: BLE001 - one user's failure must not stop the rest
        log.exception("gmail sync failed for user %s", user_id)
        return f"error: {e}"


def sync_one(settings: Settings, user_id: int) -> SyncResult | str:
    """Blocking (run in a thread): sync a single user, e.g. right after connecting."""
    from app.db import connect
    conn = connect(settings.db_path)
    try:
        return _sync_safely(conn, settings, user_id)
    finally:
        conn.close()


def sync_all(settings: Settings) -> dict[int, SyncResult | str]:
    """Blocking (run in a thread): sync every connected user. Revoked tokens are cleared."""
    from app.db import connect
    conn = connect(settings.db_path)
    try:
        return {uid: _sync_safely(conn, settings, uid) for uid in repo.list_user_ids(conn, gmail_connected=True)}
    finally:
        conn.close()
