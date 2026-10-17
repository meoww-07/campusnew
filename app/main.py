"""Entry point: FastAPI app whose lifespan starts the worker, Telegram bot and scheduler.

Run:  python -m app.main     (or: uvicorn app.main:app)
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from telegram import Update

from app import repo
from app.config import get_settings
from app.db import init_db
from app.ingest import gmail
from app.ingest.telegram_bot import TG_LIMIT, build_application
from app.llm import make_llm
from app.organize import extract_text, worker
from app.organize.indexer import FastEmbedder
from app.retrieve import reminders
from app.web.routes import STATIC, router

log = logging.getLogger("campusmind")
WEBHOOK_PATH = "/telegram/webhook"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    extract_text.configure_tesseract(settings.tesseract_cmd)
    if not extract_text.ocr_available():
        log.warning("Tesseract not found: photos and scanned PDFs will fail OCR (set TESSERACT_CMD)")
    if settings.session_secret == "dev-insecure-change-me":
        log.warning("SESSION_SECRET is the insecure default; set it in .env")

    boot = init_db(settings.db_path)  # run migrations once
    if (n := repo.requeue_interrupted_jobs(boot)):
        log.info("requeued %d job(s) interrupted by the last shutdown", n)
    boot.close()
    embedder = FastEmbedder()
    llm = make_llm(settings)
    if llm is None:
        log.warning("No LLM configured: no event extraction; /ask returns matching sources only")
    app.state.embedder, app.state.llm = embedder, llm

    # One SQLite connection per component; WAL handles concurrent access.
    stop = threading.Event()
    worker_thread = threading.Thread(target=worker.run_forever, name="worker", daemon=True,
                                     args=(init_db(settings.db_path), embedder, stop, llm))
    worker_thread.start()

    app.state.telegram = None
    if settings.telegram_mode == "webhook" and settings.telegram_bot_token and not settings.telegram_webhook_secret:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required in webhook mode")

    async def start_bot() -> None:
        """Connect to Telegram, retrying with backoff: a network hiccup must not take
        the website and worker down with it (sends wait until it's connected)."""
        delay = 5
        while True:
            tg = build_application(settings, init_db(settings.db_path), embedder, llm)
            try:
                await tg.initialize()
                await tg.start()
                if settings.telegram_mode == "webhook":
                    await tg.bot.set_webhook(url=settings.app_base_url + WEBHOOK_PATH,
                                             secret_token=settings.telegram_webhook_secret,
                                             allowed_updates=Update.ALL_TYPES)
                else:
                    await tg.bot.delete_webhook()
                    await tg.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            except Exception as e:  # noqa: BLE001 - TimedOut, NetworkError, ...
                log.warning("telegram not reachable (%s: %s); retrying in %ss", type(e).__name__, e, delay)
                try:
                    if tg.running:
                        await tg.stop()
                    await tg.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
                continue
            app.state.telegram = tg
            log.info("telegram bot started (%s)", settings.telegram_mode)
            return

    bot_task = asyncio.create_task(start_bot()) if settings.telegram_bot_token else None
    if bot_task is None:
        log.warning("TELEGRAM_BOT_TOKEN not set; bot and reminders disabled")

    async def send(chat_id: int, text: str) -> None:
        tg = app.state.telegram
        if tg is None:
            raise RuntimeError("telegram bot not connected yet")  # reminders release + retry
        await tg.bot.send_message(chat_id=chat_id, text=text[:TG_LIMIT])

    scheduler = reminders.build_scheduler(init_db(settings.db_path), send)
    if settings.gmail_configured:
        async def gmail_job() -> None:
            await asyncio.to_thread(gmail.sync_all, settings)
        scheduler.add_job(gmail_job, IntervalTrigger(minutes=15), id="gmail_sync")
    if bot_task is None:
        scheduler.remove_job("reminders")  # nothing could be delivered
        scheduler.remove_job("digest")
    scheduler.start()

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        if bot_task is not None and not bot_task.done():
            bot_task.cancel()
        tg = app.state.telegram
        if tg is not None:
            if tg.updater and tg.updater.running:
                await tg.updater.stop()
            await tg.stop()
            await tg.shutdown()
        stop.set()
        worker_thread.join(timeout=10)


app = FastAPI(title="CampusMind", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(WEBHOOK_PATH, include_in_schema=False)
async def telegram_webhook(request: Request) -> dict[str, bool]:
    settings = get_settings()
    tg = request.app.state.telegram
    if tg is None or settings.telegram_mode != "webhook":
        raise HTTPException(404)
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not settings.telegram_webhook_secret or not secrets.compare_digest(header, settings.telegram_webhook_secret):
        raise HTTPException(403, "bad secret token")
    await tg.update_queue.put(Update.de_json(await request.json(), tg.bot))
    return {"ok": True}


def run() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "apscheduler.executors.default", "googleapiclient.discovery_cache"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run()
