"""Environment-driven settings. All secrets come from .env (never hard-coded)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


NGROK_API = "http://127.0.0.1:4040/api/tunnels"


def ngrok_public_url(api: str = NGROK_API, port: int = 8000) -> str | None:
    """The https URL of a running local ngrok tunnel to `port`, or None."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(api, timeout=2) as resp:
            tunnels = json.load(resp).get("tunnels", [])
    except (OSError, ValueError):
        return None
    for t in tunnels:
        addr = str(t.get("config", {}).get("addr", ""))
        if t.get("proto") == "https" and addr.rstrip("/").endswith(str(port)):
            return t["public_url"]
    return next((t["public_url"] for t in tunnels if t.get("proto") == "https"), None)


def _base_url(raw: str | None, default: str) -> str:
    """APP_BASE_URL; the value 'ngrok' means: use the running ngrok tunnel's URL."""
    if (raw or "").strip().lower() == "ngrok":
        url = ngrok_public_url()
        if url is None:
            raise RuntimeError("APP_BASE_URL=ngrok but no ngrok tunnel is running. Start it first: ngrok http 8000")
        return url
    return (raw or default).rstrip("/")


@dataclass(frozen=True)
class Settings:
    # Telegram
    telegram_bot_token: str = ""
    telegram_mode: str = "polling"  # "polling" | "webhook"
    telegram_webhook_secret: str = ""
    # LLM (any OpenAI-compatible endpoint: OpenAI, OpenRouter, Ollama)
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    # Google OAuth / Gmail
    google_client_id: str = ""
    google_client_secret: str = ""
    gmail_query: str = "newer_than:60d"
    # Web
    app_base_url: str = "http://localhost:8000"
    session_secret: str = "dev-insecure-change-me"
    # Search
    vector_max_distance: float = 0.5
    # Storage
    db_path: Path = PROJECT_ROOT / "data" / "campusmind.db"
    files_dir: Path = PROJECT_ROOT / "data" / "files"
    # OCR: path to tesseract.exe if it's not on PATH
    tesseract_cmd: str = ""
    # Smart Spaces
    subject_aliases_path: str = ""  # default: app/subject_aliases.json
    space_similarity_max_distance: float = 0.3  # cosine distance for "similar_to" rules
    # Defaults
    default_timezone: str = "Asia/Kolkata"
    embed_dim: int = 384
    max_upload_bytes: int = 20 * 1024 * 1024

    @property
    def llm_configured(self) -> bool:
        local = any(h in self.llm_base_url for h in ("localhost", "127.0.0.1", "[::1]"))
        return bool(self.llm_base_url and self.llm_model and (self.llm_api_key or local))

    @property
    def gmail_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        g = os.getenv
        return cls(
            telegram_bot_token=g("TELEGRAM_BOT_TOKEN", ""),
            telegram_mode=g("TELEGRAM_MODE", "polling"),
            telegram_webhook_secret=g("TELEGRAM_WEBHOOK_SECRET", ""),
            llm_api_key=g("LLM_API_KEY", ""),
            llm_base_url=g("LLM_BASE_URL", cls.llm_base_url),
            llm_model=g("LLM_MODEL", cls.llm_model),
            google_client_id=g("GOOGLE_CLIENT_ID", ""),
            google_client_secret=g("GOOGLE_CLIENT_SECRET", ""),
            gmail_query=g("GMAIL_QUERY") or cls.gmail_query,
            app_base_url=_base_url(g("APP_BASE_URL"), cls.app_base_url),
            session_secret=g("SESSION_SECRET", cls.session_secret),
            vector_max_distance=_float("VECTOR_MAX_DISTANCE", 0.5),
            db_path=Path(g("DB_PATH") or cls.db_path),
            files_dir=Path(g("FILES_DIR") or cls.files_dir),
            tesseract_cmd=g("TESSERACT_CMD", ""),
            subject_aliases_path=g("SUBJECT_ALIASES_PATH", ""),
            space_similarity_max_distance=_float("SPACE_SIMILARITY_MAX_DISTANCE", 0.3),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
