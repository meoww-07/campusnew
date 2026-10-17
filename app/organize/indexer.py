"""Chunk segments and index them into FTS5 + sqlite-vec with batched embeddings."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from typing import Protocol

from app import repo
from app.models import Segment
from app.organize.chunker import chunk_text

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_BATCH = 64
# bge models expect this prefix on queries (not on documents) for retrieval.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedder:
    """fastembed wrapper; the ONNX model is loaded lazily on first use."""

    def __init__(self, model: str = EMBED_MODEL, batch_size: int = EMBED_BATCH) -> None:
        self.model_name = model
        self.batch_size = batch_size
        self._model = None
        self._lock = threading.Lock()

    def _get(self):
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding
                self._model = TextEmbedding(self.model_name)
            return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        # fastembed returns L2-normalized vectors for bge models.
        return [v.tolist() for v in self._get().embed(list(texts), batch_size=self.batch_size)]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([QUERY_PREFIX + text])[0]


def index_segments(
    conn: sqlite3.Connection, user_id: int, segments: Sequence[Segment], embedder: Embedder,
    embed_context: dict[int, str] | None = None,
) -> int:
    """Chunk every segment, embed all chunks in one batched pass, then store.

    `embed_context` maps segment id -> text prepended ONLY for embedding (e.g.
    the chat message being replied to); stored chunk text and FTS are unchanged.
    Returns the number of chunks indexed.
    """
    ctx = embed_context or {}
    plan = [(seg, chunk_text(seg.text)) for seg in segments]
    flat = [(ctx[seg.id] + "\n" if seg.id in ctx else "") + c.text for seg, chunks in plan for c in chunks]
    vectors = embedder.embed(flat)
    pos = 0
    for seg, chunks in plan:
        if chunks:
            repo.add_chunks(conn, user_id, seg.id, chunks, vectors[pos:pos + len(chunks)])
            pos += len(chunks)
    return pos
