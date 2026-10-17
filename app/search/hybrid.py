"""Hybrid search: FTS5 (bm25) + sqlite-vec (cosine) fused with Reciprocal Rank Fusion."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from pydantic import BaseModel, Field

from app import repo
from app.models import Category, ChunkHit, SourceType
from app.organize.indexer import Embedder

CANDIDATES = 50
RRF_K = 60

# Function words that only add noise to an OR query.
STOPWORDS = frozenset("""
a an the is are was were be been am do does did of to in on at for from by with and or not no
what when where which who whom why how whats is it its this that these those there here i me my
we our you your he she they them their his her can could should would will shall may might
any some about into than then so if as up out over please tell show give find get let know
""".split())
_WORD = re.compile(r"\w+", re.UNICODE)


class SearchFilters(BaseModel):
    source_types: list[SourceType] | None = None
    start: datetime | None = None  # observed_at >= start
    end: datetime | None = None  # observed_at <= end
    category: Category | None = None
    space_id: int | None = None  # only segments in this space


class SearchResult(BaseModel):
    hit: ChunkHit
    score: float  # RRF score
    fts_rank: int | None = None
    vec_distance: float | None = None
    extra: dict = Field(default_factory=dict)


def sanitize_fts(query: str) -> str | None:
    """Turn free text into a safe FTS5 query: quoted terms joined by OR.

    Quoting neutralizes FTS5 syntax (AND/NEAR/*, column filters, quotes).
    Returns None if nothing searchable remains.
    """
    words = [w.lower() for w in _WORD.findall(query)]
    terms = [w for w in words if w not in STOPWORDS] or words
    seen: list[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    return " OR ".join(f'"{t}"' for t in seen[:32]) or None


def _passes(hit: ChunkHit, f: SearchFilters | None, in_space: set[int] | None = None) -> bool:
    if f is None:
        return True
    if in_space is not None and hit.segment_id not in in_space:
        return False
    if f.source_types and hit.source_type not in f.source_types:
        return False
    if f.category and hit.category != f.category:
        return False
    if f.start and hit.observed_at < repo.iso(f.start):
        return False
    if f.end and hit.observed_at > repo.iso(f.end):
        return False
    return True


def search(
    conn: sqlite3.Connection,
    embedder: Embedder,
    user_id: int,
    query: str,
    k: int = 10,
    filters: SearchFilters | None = None,
    max_distance: float = 0.5,
) -> list[SearchResult]:
    if not query.strip():
        return []
    match = sanitize_fts(query)
    fts = repo.fts_search(conn, user_id, match, CANDIDATES) if match else []
    vec = [(cid, d) for cid, d in repo.vec_search(conn, user_id, embedder.embed_query(query), CANDIDATES)
           if d <= max_distance]

    hits = repo.get_chunk_hits(conn, user_id, list({cid for cid, _ in fts} | {cid for cid, _ in vec}))
    in_space = repo.space_segment_ids(conn, user_id, filters.space_id) if filters and filters.space_id else None
    fts_ids = [cid for cid, _ in fts if cid in hits and _passes(hits[cid], filters, in_space)]
    vec_ids = [cid for cid, _ in vec if cid in hits and _passes(hits[cid], filters, in_space)]
    dist = dict(vec)

    scores: dict[int, float] = {}
    for ranked in (fts_ids, vec_ids):
        for rank, cid in enumerate(ranked, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    fts_rank = {cid: r for r, cid in enumerate(fts_ids, start=1)}

    # Ties (common for single-list hits) go to the newer item.
    order = sorted(scores, key=lambda c: (-round(scores[c], 12), _neg_time(hits[c].observed_at)))
    return [
        SearchResult(hit=hits[c], score=scores[c], fts_rank=fts_rank.get(c), vec_distance=dist.get(c))
        for c in order[:k]
    ]


def _neg_time(iso_ts: str) -> float:
    return -datetime.fromisoformat(iso_ts).timestamp()
