# CampusMind

A persistent memory for college students. Forward exam dates, deadlines and notices from Telegram, WhatsApp exports, Gmail and PDFs. CampusMind organizes them, answers questions with cited sources, and reminds you before deadlines.

**Interfaces:** a Telegram bot and one web page.
**Storage:** a single SQLite file (FTS5 + sqlite-vec).

```
Telegram / WhatsApp .txt / Gmail / web upload
        │  (source + segments + job, one transaction → "Saved ✓")
        ▼
worker: PDF text (OCR fallback) → chunks → FTS5 + bge-small embeddings
        → LLM extraction (summary, category, events) → conflicts
        ▼
hybrid search (bm25 + cosine, RRF) → /ask answers with [n] citations

scheduler: 24h / 2h reminders · 07:30 digest · lifecycle · Gmail sync
```

## Features

| Area | What it does |
|---|---|
| **Telegram** | `/start`, save text and forwards (with the original date and sender), PDFs, photos (OCR), WhatsApp `.txt` exports. `/ask <question>` or a message starting with `?`. `/upcoming`, `/web` (one-time login link), `/gmail`. |
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
.venv\Scripts\activate        # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env        # macOS/Linux: cp .env.example .env
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

Use any OpenAI-compatible endpoint (see `.env.example`).

Fully local with Ollama:

```bash
ollama pull qwen2.5:7b
```

Then set `LLM_BASE_URL=http://localhost:11434/v1` and `LLM_MODEL=qwen2.5:7b`; no key is needed.

Without an LLM, ingestion and search still work, but no events are extracted and `/ask` lists matching sources instead of writing an answer.
