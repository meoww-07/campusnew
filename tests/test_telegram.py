import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from telegram import Chat, MessageOriginChannel, MessageOriginHiddenUser, MessageOriginUser
from telegram import User as TgUser

from app import repo
from app.config import Settings
from app.ingest import telegram_bot as bot
from app.models import EventIn
from tests.fakes import FakeDownloadable, FakeMessage, make_context, make_pdf, make_update

FWD_DATE = datetime(2026, 10, 10, 18, 30, tzinfo=timezone.utc)


@pytest.fixture
def deps(conn, tmp_path):
    return bot.Deps(conn=conn, settings=Settings(files_dir=tmp_path / "files", app_base_url="https://cm.test"))


def run(handler, msg, deps, args=None):
    asyncio.run(handler(make_update(msg), make_context(deps, args)))
    return [r[0] for r in msg.replies]


# ---------- forward metadata (real PTB objects) ----------

def test_forward_info_variants():
    plain = FakeMessage("x")
    assert bot.forward_info(plain) == (plain.date, None)
    user = MessageOriginUser(date=FWD_DATE, sender_user=TgUser(5, "Prof", False, last_name="Rao"))
    assert bot.forward_info(FakeMessage("x", forward_origin=user)) == (FWD_DATE, "Prof Rao")
    hidden = MessageOriginHiddenUser(date=FWD_DATE, sender_user_name="CR Riya")
    assert bot.forward_info(FakeMessage("x", forward_origin=hidden))[1] == "CR Riya"
    chan = MessageOriginChannel(date=FWD_DATE, chat=Chat(-100, "channel", title="Exam Cell"), message_id=3)
    assert bot.forward_info(FakeMessage("x", forward_origin=chan))[1] == "Exam Cell"


# ---------- text ----------

def test_start_registers_user(deps):
    replies = run(bot.start, FakeMessage("/start", chat_id=777), deps)
    assert repo.get_user_by_chat_id(deps.conn, 777).name == "Asha"
    assert "CampusMind" in replies[0]


def test_text_and_forward_are_saved(deps):
    origin = MessageOriginHiddenUser(date=FWD_DATE, sender_user_name="CR Riya")
    msg = FakeMessage("OS quiz moved to Thursday 3pm", message_id=42, forward_origin=origin)
    assert run(bot.text_message, msg, deps) == [bot.SAVED]
    assert msg.replies[0][1]["do_quote"] is True
    user = repo.get_user_by_chat_id(deps.conn, 1001)
    [src] = repo.list_sources(deps.conn, user.id)
    assert src.type == "telegram" and src.external_id == "1001:42"
    [s] = repo.list_segments(deps.conn, user.id, src.id)
    assert s.locator == {"chat_id": 1001, "message_id": 42, "forward_sender": "CR Riya"}
    assert s.observed_at == "2026-10-10T18:30:00+00:00"  # forward origin date, not message date
    assert repo.claim_job(deps.conn).source_id == src.id  # queued
    # Same content again => dedup
    assert run(bot.text_message, FakeMessage("OS quiz moved to  Thursday 3pm", message_id=43), deps) == [bot.ALREADY_SAVED]


def test_question_mark_routes_to_ask_not_storage(deps):
    from tests.fakes import FakeEmbedder
    deps.embedder = FakeEmbedder()
    replies = run(bot.text_message, FakeMessage("? when is the OS quiz"), deps)
    assert replies[0] == "I couldn't find that in your saved info."
    assert deps.conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 0


def test_ask_replies_to_cited_telegram_message(deps):
    from app.organize import worker
    from tests.fakes import FakeEmbedder, FakeLLM
    deps.embedder = FakeEmbedder()
    deps.llm = FakeLLM("The OS quiz is on Thursday at 3pm [1].")
    run(bot.text_message, FakeMessage("OS quiz moved to Thursday 3pm", message_id=42), deps)
    worker.drain(deps.conn, deps.embedder)
    msg = FakeMessage("/ask when is the OS quiz?")
    run(bot.ask, msg, deps, args=["when", "is", "the", "OS", "quiz?"])
    text, kwargs = msg.replies[0]
    assert text.startswith("The OS quiz is on Thursday at 3pm [1].\n\nSources:\n[1] Telegram")
    assert kwargs["reply_parameters"].message_id == 42


# ---------- documents + photos ----------

def _doc(data: bytes, name: str, size: int | None = None):
    return FakeDownloadable(data=data, file_name=name, file_size=size if size is not None else len(data))


def test_pdf_document_saved(deps):
    msg = FakeMessage(document=_doc(make_pdf(["Exam circular"]), "circular.pdf"), caption="FYI")
    assert run(bot.document_message, msg, deps)[0].startswith(bot.SAVED)
    user = repo.get_user_by_chat_id(deps.conn, 1001)
    [src] = repo.list_sources(deps.conn, user.id)
    assert src.title == "circular.pdf"
    job = repo.claim_job(deps.conn)
    assert job.kind == "pdf" and job.payload["caption"] == "FYI"


def test_document_rejections(deps):
    big = FakeMessage(document=_doc(b"%PDF-", "big.pdf", size=25 * 1024 * 1024))
    assert "too large" in run(bot.document_message, big, deps)[0]
    exe = FakeMessage(document=_doc(b"MZ\x90\x00\x03\x00", "notes.pdf"))  # lying extension
    assert run(bot.document_message, exe, deps)[0] == bot.UNSUPPORTED
    notes = FakeMessage(document=_doc(b"my todo list\nbuy milk", "notes.txt"))
    assert "doesn't look like a WhatsApp chat export" in run(bot.document_message, notes, deps)[0]
    assert deps.conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 0


def test_whatsapp_txt_is_imported(deps):
    data = "12/10/26, 9:15 pm - Riya: DBMS midsem on 21 Oct\n12/10/26, 9:16 pm - Arjun: thanks\n".encode()
    msg = FakeMessage(document=_doc(data, "WhatsApp Chat with CSE A.txt"))
    assert run(bot.document_message, msg, deps)[0] == f"{bot.SAVED} Imported 2 messages, organizing them now…"
    user = repo.get_user_by_chat_id(deps.conn, 1001)
    [src] = repo.list_sources(deps.conn, user.id)
    assert src.type == "whatsapp" and src.title == "WhatsApp: CSE A"
    assert run(bot.document_message, FakeMessage(document=_doc(data, "again.txt")), deps)[0] == bot.ALREADY_SAVED


def test_photo_uses_largest_size(deps):
    from tests.test_extract_upload import _png
    small = FakeDownloadable(data=b"not used")
    large = FakeDownloadable(data=_png("notice"))
    msg = FakeMessage(photo=[small, large], message_id=5)
    assert run(bot.photo_message, msg, deps)[0].startswith(bot.SAVED)
    assert repo.claim_job(deps.conn).kind == "image"


# ---------- commands ----------

def test_upcoming_lists_next_7_days(deps):
    user = repo.upsert_user(deps.conn, 1001, "Asha")
    res = repo.ingest_source(deps.conn, user.id, "telegram", "h", [])
    seg_id = repo.add_segments(deps.conn, user.id, res.source_id, [
        __import__("app.models", fromlist=["SegmentIn"]).SegmentIn(text="t", observed_at=FWD_DATE)])[0]
    now = datetime.now(timezone.utc)
    for title, days in (("DBMS midsem", 2), ("Far away", 20)):
        repo.add_event(deps.conn, user.id, EventIn(segment_id=seg_id, title=title, type="exam", subject=title,
                                                   due_at=now + timedelta(days=days), confidence=0.9))
    [reply] = run(bot.upcoming, FakeMessage("/upcoming"), deps)
    assert "DBMS midsem (exam)" in reply and "Far away" not in reply


def test_web_and_gmail_links_carry_one_time_token(deps):
    [reply] = run(bot.web, FakeMessage("/web"), deps)
    token = reply.split("token=")[1]
    assert reply.startswith("Your login link") and "https://cm.test/login?token=" in reply
    assert repo.consume_login_token(deps.conn, token) == repo.get_user_by_chat_id(deps.conn, 1001).id
    [reply] = run(bot.gmail, FakeMessage("/gmail"), deps)
    assert "isn't set up" in reply
    deps.settings = replace(deps.settings, google_client_id="id", google_client_secret="s")
    [reply] = run(bot.gmail, FakeMessage("/gmail"), deps)
    assert "https://cm.test/gmail/connect?token=" in reply


# ---------- webhook secret ----------

def test_webhook_requires_secret(monkeypatch):
    from app import main
    s = Settings(telegram_mode="webhook", telegram_webhook_secret="s3cret")
    monkeypatch.setattr(main, "get_settings", lambda: s)
    queue: asyncio.Queue = asyncio.Queue()

    class FakeApp:
        update_queue = queue
        bot = None

    main.app.state.telegram = FakeApp()
    client = TestClient(main.app)  # no `with`: lifespan (bot/worker) is not started
    body = {"update_id": 1}
    assert client.post(main.WEBHOOK_PATH, json=body).status_code == 403
    assert client.post(main.WEBHOOK_PATH, json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}).status_code == 403
    ok = client.post(main.WEBHOOK_PATH, json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
    assert ok.status_code == 200 and queue.qsize() == 1
    main.app.state.telegram = None
