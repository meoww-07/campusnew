"""Test doubles: a deterministic embedder and minimal Telegram objects."""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import fitz

from app.db import EMBED_DIM


class FakeEmbedder:
    """Hashed bag-of-words, L2-normalized: shared words => closer vectors."""

    def __init__(self) -> None:
        self.calls: list[int] = []  # batch sizes, to assert batching

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * EMBED_DIM
        for w in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % EMBED_DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def make_pdf(pages: Sequence[str | None]) -> bytes:
    """A PDF where each entry is page text, or None for a blank (scanned-like) page."""
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(fitz.Rect(50, 50, 550, 800), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


class FakeFile:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self.data)


class FakeDownloadable(SimpleNamespace):
    async def get_file(self) -> FakeFile:
        return FakeFile(self.data)


class FakeMessage:
    def __init__(self, text: str | None = None, *, chat_id: int = 1001, message_id: int = 1,
                 date: datetime | None = None, forward_origin: Any = None, caption: str | None = None,
                 document: Any = None, photo: list[Any] | None = None) -> None:
        self.text = text
        self.chat_id = chat_id
        self.message_id = message_id
        self.date = date or datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
        self.forward_origin = forward_origin
        self.caption = caption
        self.document = document
        self.photo = photo
        self.replies: list[tuple[str, dict[str, Any]]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append((text, kwargs))


class FakeLLM:
    """Returns canned replies (callable or list) and records prompts."""

    def __init__(self, replies: Any = None) -> None:
        self.replies = replies
        self.prompts: list[tuple[str, str, bool]] = []

    def chat(self, system: str, user: str, *, json_mode: bool = False) -> str:
        self.prompts.append((system, user, json_mode))
        if callable(self.replies):
            return self.replies(system, user)
        if isinstance(self.replies, list):
            return self.replies.pop(0)
        return self.replies or ""


def make_update(msg: FakeMessage, name: str = "Asha") -> SimpleNamespace:
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=msg.chat_id),
        effective_user=SimpleNamespace(full_name=name),
    )


def make_context(deps: Any, args: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(bot_data={"deps": deps}, args=args or [])
