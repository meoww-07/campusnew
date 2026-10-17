"""Telegram handlers: storage of text/forwards/PDFs/photos, plus commands.

Handlers only do quick DB writes; OCR, chunking and embeddings run in the
worker. "Saved ✓" is sent only after the ingest transaction commits.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from telegram import LinkPreviewOptions, Message, ReplyParameters, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app import repo
from app.config import Settings
from app.ingest import whatsapp
from app.ingest.upload import UploadRejected, detect_kind, store_file
from app.llm import LLM
from app.models import SegmentIn, User
from app.organize.indexer import Embedder
from app.retrieve import answer as answering

log = logging.getLogger(__name__)

SAVED = "Saved ✓"
ALREADY_SAVED = "Already saved ✓"
HELP = (
    "Send or forward me anything important: messages, PDFs, photos of notices.\n\n"
    "/ask <question> or start a message with ? to search\n"
    "/upcoming – events in the next 7 days\n"
    "/web – open your dashboard\n"
    "/gmail – connect Gmail"
)
UNSUPPORTED = "I can save text, PDFs, photos, and WhatsApp chat exports (.txt). That file type isn't supported."


TG_LIMIT = 4000


@dataclass
class Deps:
    conn: sqlite3.Connection
    settings: Settings
    embedder: Embedder | None = None
    llm: LLM | None = None


def _deps(context: ContextTypes.DEFAULT_TYPE) -> Deps:
    return context.bot_data["deps"]


def _user(update: Update, deps: Deps) -> User:
    """Look up the sender, registering on first contact."""
    chat = update.effective_chat
    u = update.effective_user
    return repo.upsert_user(deps.conn, chat.id, u.full_name if u else None)


def forward_info(message: Message) -> tuple[datetime, str | None]:
    """(observed_at, forward_sender). Forward origin date wins over message date."""
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return message.date, None
    sender: str | None = None
    if getattr(origin, "sender_user", None) is not None:
        sender = origin.sender_user.full_name
    elif getattr(origin, "sender_user_name", None):
        sender = origin.sender_user_name
    elif getattr(origin, "sender_chat", None) is not None:
        sender = origin.sender_chat.title
    elif getattr(origin, "chat", None) is not None:
        sender = origin.chat.title
    return origin.date, sender


def _locator(message: Message, forward_sender: str | None) -> dict[str, Any]:
    return {"chat_id": message.chat_id, "message_id": message.message_id, "forward_sender": forward_sender}


# ---------- commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _user(update, _deps(context))
    await update.effective_message.reply_text(
        f"Hi {user.name or 'there'}! I'm CampusMind.\n\n{HELP}\n\n(Your chat id is {user.telegram_chat_id}.)")


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    question = " ".join(context.args or []) if msg.text.startswith("/") else msg.text.lstrip("?").strip()
    if not question:
        await msg.reply_text("Ask me something, e.g. /ask when is the DBMS exam?")
        return
    deps = _deps(context)
    user = _user(update, deps)
    if deps.embedder is None:
        await msg.reply_text("Search isn't available right now.")
        return
    result = await asyncio.to_thread(
        answering.ask, deps.conn, deps.embedder, deps.llm, user.id, question,
        tz_name=user.timezone, max_distance=deps.settings.vector_max_distance,
    )
    await msg.reply_text(**format_answer(result, msg.chat_id))


def format_answer(result: answering.Answer, chat_id: int) -> dict[str, Any]:
    """reply_text kwargs: answer + numbered sources, replying to the cited message if possible."""
    text = result.answer
    if result.citations:
        text += "\n\nSources:\n" + "\n".join(f"[{c.n}] {c.label}" for c in result.citations)
    if len(text) > TG_LIMIT:
        text = text[:TG_LIMIT - 1] + "…"
    kwargs: dict[str, Any] = {"text": text}
    target = answering.telegram_reply_target(result, chat_id)
    if target is not None:
        kwargs["reply_parameters"] = ReplyParameters(message_id=target, allow_sending_without_reply=True)
    return kwargs


async def upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    user = _user(update, deps)
    now = datetime.now(timezone.utc)
    events = repo.list_events(deps.conn, user.id, start=now, end=now + timedelta(days=7))
    moved = answering.moved_from(deps.conn, user.id, events, user.timezone)
    await update.effective_message.reply_text(format_events(events, user.timezone, moved) or "Nothing in the next 7 days 🎉")


def format_events(events, tz_name: str, moved: dict[int, str] | None = None) -> str:
    tz = ZoneInfo(tz_name)
    lines = []
    for e in events:
        due = datetime.fromisoformat(e.due_at).astimezone(tz)
        when = due.strftime("%a %d %b") + ("" if e.all_day else due.strftime(", %H:%M"))
        flag = " ⚠️ conflicting dates" if e.status == "conflict" else ""
        note = f" 🔁 {moved[e.id]}" if moved and e.id in moved else ""
        lines.append(f"• {when} — {e.title} ({e.type}){flag}{note}")
    return "\n".join(lines)


async def web(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    user = _user(update, deps)
    token = repo.create_login_token(deps.conn, user.id)
    await update.effective_message.reply_text(
        f"Your login link (valid 15 min, one use):\n{deps.settings.app_base_url}/login?token={token}"
   ,
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


async def gmail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    user = _user(update, deps)
    if not deps.settings.gmail_configured:
        await update.effective_message.reply_text("Gmail isn't set up on this server yet "
                                                  "(GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
        return
    if user.gmail_refresh_token:
        last = f" Last sync: {answering.fmt_time(user.gmail_last_sync, user.timezone)}." if user.gmail_last_sync else ""
        await update.effective_message.reply_text(f"Gmail is already connected ✓{last} "
                                                  "Use the link below only to reconnect.")
    token = repo.create_login_token(deps.conn, user.id)
    await update.effective_message.reply_text(
        f"Connect Gmail (read-only, link valid 15 min):\n{deps.settings.app_base_url}/gmail/connect?token={token}"
   ,
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


# ---------- storage ----------

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg.text.lstrip().startswith("?"):
        await ask(update, context)
        return
    deps = _deps(context)
    user = _user(update, deps)
    observed_at, sender = forward_info(msg)
    text = msg.text.strip()
    res = repo.ingest_source(
        deps.conn, user.id, "telegram", repo.content_hash(text),
        [SegmentIn(text=text, observed_at=observed_at, locator=_locator(msg, sender))],
        external_id=f"{msg.chat_id}:{msg.message_id}",
        title=(f"Fwd from {sender}: " if sender else "") + text[:60],
    )
    await msg.reply_text(SAVED if res.created else ALREADY_SAVED, do_quote=True)


async def document_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    deps = _deps(context)
    doc = msg.document
    limit = deps.settings.max_upload_bytes
    if doc.file_size and doc.file_size > limit:
        await msg.reply_text(f"That file is too large (max {limit // (1024 * 1024)} MB).", do_quote=True)
        return
    user = _user(update, deps)
    data = bytes(await (await doc.get_file()).download_as_bytearray())
    kind = detect_kind(data)
    if kind == "txt":
        res = whatsapp.import_export(deps.conn, user.id, data, doc.file_name)
        if res is None:
            await msg.reply_text("That .txt doesn't look like a WhatsApp chat export. In WhatsApp: "
                                 "open the chat → ⋮ → More → Export chat → Without media.", do_quote=True)
        elif res.created:
            n = len(repo.list_segments(deps.conn, user.id, res.source_id))
            await msg.reply_text(f"{SAVED} Imported {n} messages, organizing them now…", do_quote=True)
        else:
            await msg.reply_text(ALREADY_SAVED, do_quote=True)
        return
    if kind != "pdf":
        await msg.reply_text(UNSUPPORTED, do_quote=True)
        return
    await _store(msg, deps, user, data, doc.file_name)


async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    deps = _deps(context)
    user = _user(update, deps)
    largest = msg.photo[-1]
    data = bytes(await (await largest.get_file()).download_as_bytearray())
    await _store(msg, deps, user, data, f"photo_{msg.message_id}.jpg")


async def _store(msg: Message, deps: Deps, user: User, data: bytes, filename: str | None) -> None:
    observed_at, sender = forward_info(msg)
    try:
        res = store_file(
            deps.conn, deps.settings.files_dir, user.id, data,
            source_type="telegram", filename=filename, observed_at=observed_at,
            locator=_locator(msg, sender), caption=msg.caption,
            external_id=f"{msg.chat_id}:{msg.message_id}",
            max_bytes=deps.settings.max_upload_bytes,
        )
    except UploadRejected as e:
        await msg.reply_text(str(e), do_quote=True)
        return
    await msg.reply_text(f"{SAVED} Reading it now…" if res.created else ALREADY_SAVED, do_quote=True)


async def unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(UNSUPPORTED)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("telegram handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Sorry, something went wrong. Please try again.")


def build_application(settings: Settings, conn: sqlite3.Connection,
                      embedder: Embedder | None = None, llm: LLM | None = None) -> Application:
    builder = (Application.builder().token(settings.telegram_bot_token)
               .connect_timeout(20).read_timeout(30).write_timeout(30).pool_timeout(10)
               .get_updates_connect_timeout(20).get_updates_read_timeout(40))
    if settings.telegram_mode == "webhook":
        builder = builder.updater(None)  # updates are pushed in by FastAPI
    app = builder.build()
    app.bot_data["deps"] = Deps(conn=conn, settings=settings, embedder=embedder, llm=llm)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("upcoming", upcoming))
    app.add_handler(CommandHandler("web", web))
    app.add_handler(CommandHandler("gmail", gmail))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.add_handler(MessageHandler(filters.Document.ALL, document_message))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message))
    app.add_handler(MessageHandler(~filters.COMMAND, unsupported))
    app.add_error_handler(on_error)
    return app
