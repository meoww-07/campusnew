# CampusMind

A persistent memory for college students. Forward exam dates, deadlines and notices from Telegram, WhatsApp exports, Gmail and PDFs. CampusMind organizes them, answers questions with cited sources, and reminds you before deadlines.

**Interfaces:** a Telegram bot and one web page.
**Storage:** a single SQLite file (FTS5 + sqlite-vec).

```
Telegram / WhatsApp .txt / Gmail / web upload
        │   (source + segments + job, one transaction → "Saved ✓")
        ▼
   worker: PDF text (OCR fallback) → chunks → FTS5 + bge-small embeddings
           → LLM extraction (summary, category, events) → conflicts
        ▼
   hybrid search (bm25 + cosine, RRF)  →  /ask answers with [n] citations
   scheduler: 24h / 2h reminders · 07:30 digest · lifecycle · Gmail sync
```

## Table of contents

- [Features](#features)
- [Setup](#setup)
- [Tests and evaluation](#tests-and-evaluation)
- [Project layout](#project-layout)
- [Design notes](#design-notes)

## Features

| Area | What it does |
|---|---|
| **Telegram** | `/start`, save text and forwards (with the original date and sender), PDFs, photos (OCR), WhatsApp `.txt` exports. `/ask <q>` or a message starting with `?`. `/upcoming`, `/web` (one-time login link), `/gmail`. |
| **WhatsApp** | Parses Android and iOS exports: dd/mm/yy or dd/mm/yyyy, 12h/24h, narrow no-break spaces, multi-line messages. Skips system and media lines. |
| **Gmail** | OAuth (read-only), syncs every 15 min, MIME parsing, cleans quoted replies, signatures and disclaimers. PDF and image attachments are stored with their email. |
| **Organizing** | Page-level PDF segments, OCR for scanned pages, sentence-aware ~400-token chunks with 50 overlap. LLM extraction: dates resolved against the *message* date, plus a deterministic weekday/year fix-up. |
| **Conflicts** | "DBMS midsem 21 Oct" plus a later "postponed to 23 Oct" marks both as `conflict`; nothing is deleted. Answers show both, with dates. |
| **Search** | FTS5 bm25 + sqlite-vec cosine, Reciprocal Rank Fusion (k=60), filters (source type, date range, category), distance cutoff. |
| **Reminders** | 24h and 2h before each event (sent at most once), a daily 07:30 digest (Asia/Kolkata), past events archived. |
| **Web** | Ask with source cards, upcoming events with conflicts highlighted, my sources (with delete), upload, Connect Gmail. |
| **Security** | Secrets in `.env`; every DB query scoped by `user_id` (tested); webhook secret header verified; uploads checked by magic bytes (PDF, PNG, JPG, TXT); signed HTTP-only session cookie plus a CSRF header. |

## Setup

### 1. Python and dependencies

Python 3.11+ (developed on 3.12).

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env
```

The embedding model (BAAI/bge-small-en-v1.5, about 130 MB) downloads on first use.

> **Windows note:** on OneDrive or app-virtualized folders, `python -m venv` can fail to install pip, or hit path-length limits. If that happens, create the venv somewhere short such as `C:\Users\<you>\.venvs\campusmind`.

### 2. Telegram bot (BotFather)

1. In Telegram, open **@BotFather**, send `/newbot`, and pick a name and a username ending in `bot`.
2. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN=`.
3. Optional: send `/setcommands` to BotFather and paste:

   ```
   ask - Ask about your saved info
   upcoming - Events in the next 7 days
   web - Get a login link for the web page
   gmail - Connect Gmail
   ```

4. Leave `TELEGRAM_MODE=polling` for local development.

### 3. LLM

Use any OpenAI-compatible endpoint (see `.env.example`). Fully local with Ollama:

```bash
ollama pull qwen2.5:7b
```

Then set `LLM_BASE_URL=http://localhost:11434/v1` and `LLM_MODEL=qwen2.5:7b`; no key is needed.

Without an LLM, ingestion and search still work, but no events are extracted and `/ask` lists matching sources instead of writing an answer.

### 4. Tesseract (OCR for photos and scanned PDFs)

- **Windows:** `winget install UB-Mannheim.TesseractOCR` (or the installer from [github.com/UB-Mannheim/tesseract/wiki](https://github.com/UB-Mannheim/tesseract/wiki)). The default install location `C:\Program Files\Tesseract-OCR` is found automatically. Set `TESSERACT_CMD` only for a custom location.
- **macOS:** `brew install tesseract`
- **Ubuntu/Debian:** `sudo apt install tesseract-ocr`

OCR flattens uneven lighting and takes a majority vote over four preprocessing variants. On phone-style notice photos that raised exact date+time accuracy from 16/20 to 18/20. Still double-check dates read from blurry photos.

Without Tesseract, text PDFs still work. Photos fail with a clear error on the job, and scanned pages keep only the little text they have.

### 5. Gmail (Google Cloud OAuth, testing mode)

1. Go to <https://console.cloud.google.com/>, create a project, open **APIs & Services → Library** and enable the **Gmail API**.
2. **OAuth consent screen:** user type **External**. Fill in the app name and support email, and add the scope `https://www.googleapis.com/auth/gmail.readonly`. Leave publishing status as **Testing** and add your Google account(s) under **Test users**. Only test users can connect while in testing mode.
3. **Credentials → Create credentials → OAuth client ID → Web application.** Add an *Authorized redirect URI* of `{APP_BASE_URL}/gmail/callback`, e.g. `http://localhost:8000/gmail/callback`. Add the ngrok one too if you use it.
4. Put the client ID and secret into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
5. In Telegram, send `/gmail` (or click **Connect Gmail** on the web page) and approve.

> **Note:** Google shows an "unverified app" warning in testing mode: choose *Continue*. In testing mode Google expires refresh tokens after 7 days; when that happens CampusMind clears the token and you run `/gmail` again.

### 6. Run

```bash
python -m app.main
```

This starts FastAPI on port 8000 plus the worker, bot and scheduler. In Telegram, send `/start` to your bot, then forward it something. Send `/web` for a login link to the page.

Load the sample data for your Telegram chat, so you have something to ask about right away:

```bash
python -m app.seed <your_telegram_chat_id>
```

The bot's `/start` reply shows your chat id. Seeding while the app runs is fine; add `--no-process` to let the running worker do the processing.

### 7. Webhook mode with ngrok (optional)

Polling is simplest for development. To receive updates by webhook instead:

```bash
ngrok http 8000
```

Then in `.env`:

```
APP_BASE_URL=https://<your-subdomain>.ngrok-free.app
TELEGRAM_MODE=webhook
TELEGRAM_WEBHOOK_SECRET=<long random string>
```

On startup the app calls `setWebhook` with that secret. Every update must carry the matching `X-Telegram-Bot-Api-Secret-Token` header or it is rejected with 403. If you use Gmail, also add the ngrok redirect URI in Google Cloud.

## Tests and evaluation

```bash
pytest -m "not slow"          # ~140 fast tests, no network
pytest                        # also runs the real embedding model
python -m app.eval            # recall@5 on seed/eval.jsonl + answers for manual review
python -m app.eval --no-llm   # search-only
```

On the seed data (an 80-message WhatsApp group export with a rescheduled exam plus 2 circular PDFs, 25 questions): **recall@5 = 0.92** with `VECTOR_MAX_DISTANCE=0.5`. Cutoffs 0.3–0.4 drop a correct hit, and 0.6–0.7 gain nothing. The two misses are known limitations:

- **A chat reply that doesn't repeat the question's words** ("Yes, up to BCNF"). Mitigation in place: chat messages are embedded together with the previous message, and `/ask` attaches the following message to a question hit, so the *answer* is still correct.
- **A short rule inside a long PDF page.** bm25 length normalization penalizes the ~300-token page next to short chat messages. Smaller chunks for PDFs would help.

## Project layout

```
app/
  config.py db.py models.py repo.py llm.py main.py seed.py eval.py
  ingest/   telegram_bot.py whatsapp.py gmail.py upload.py
  organize/ extract_text.py chunker.py llm_extract.py indexer.py worker.py
  search/   hybrid.py
  retrieve/ answer.py reminders.py
  web/      routes.py static/index.html
tests/      one file per component (+ user isolation tests)
seed/       WhatsApp export, 2 circular PDFs, eval.jsonl, build_seed.py
```

## Design notes

- **One write transaction per ingest:** source + segments + job commit together. The bot replies "Saved ✓" only after the commit. Duplicates (same user + content hash) reply "Already saved ✓".
- **Idempotent worker:** jobs are claimed atomically, stale locks (over 10 min) are reclaimed, and a job gets at most 3 attempts. Text extraction, indexing and LLM extraction each skip work already done, so a retry never duplicates rows. One bad message doesn't block the others in the same WhatsApp export.
- **Dates:** stored in UTC, displayed in the user's timezone (default Asia/Kolkata). The LLM resolves dates against `observed_at`. Post-processing then fixes weekday miscounts, applies the missing-year rule, drops dates far out of range, and drops events with confidence < 0.5 or no date.
- **Superseding:** when a *newer* message announces a change ("postponed to…", "rescheduled", "deadline extended", "UPDATE"), its date replaces the old one automatically. The old event is kept as `cancelled` with `superseded_by` set, and gets no reminders. Answers, `/upcoming` and reminders say "moved from …". Only when there's no such wording are both dates flagged `conflict` for you to decide. `repo.undo_supersede` restores both ("not an update").
- **Conflicts** need the same subject (so "DBMS" matches "Database Management Systems"), the same assessment (quiz 2 ≠ quiz 1, viva ≠ midsem) and a genuinely different time. An all-day "22 Oct" and "22 Oct 10 am" are the same event.
- **Delete** removes the source, segments, chunks, FTS rows, vectors, events, reminder records and the stored file/attachment folder.
- **Migrations** are append-only SQL in `app/db.py`, tracked with `PRAGMA user_version`.
