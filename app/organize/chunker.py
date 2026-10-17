"""Sentence-boundary chunking with token budgets and char offsets.

Tokens are approximated as words/punctuation (close enough to WordPiece
for budgeting; bge-small truncates at 512 so ~400 leaves headroom).
"""
from __future__ import annotations

import re

from app.models import ChunkIn

MAX_TOKENS = 400
OVERLAP_TOKENS = 50

_TOKEN = re.compile(r"\w+|[^\w\s]")
# A sentence ends at . ! ? (plus closing quotes/brackets) followed by space, or at a newline.
_SENTENCE = re.compile(r"[^\n.!?]*(?:[.!?]+[\"')\]]*(?=\s|$)|\n|$)")


def count_tokens(text: str) -> int:
    return len(_TOKEN.findall(text))


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for m in _SENTENCE.finditer(text):
        start, end = m.start(), m.end()
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end > start:
            spans.append((start, end))
    return spans


def _split_long(text: str, start: int, end: int, max_tokens: int) -> list[tuple[int, int]]:
    """Hard-split a single over-long sentence on token boundaries."""
    toks = list(_TOKEN.finditer(text, start, end))
    return [
        (toks[i].start(), toks[min(i + max_tokens, len(toks)) - 1].end())
        for i in range(0, len(toks), max_tokens)
    ]


LINE_CHUNK_TOKENS = 90


def _line_spans(text: str) -> list[tuple[int, int]]:
    spans, pos = [], 0
    for line in text.split("\n"):
        start, end = pos, pos + len(line)
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end > start:
            spans.append((start, end))
        pos += len(line) + 1
    return spans


def is_structured(text: str) -> bool:
    """Tables, rubrics, timetables, lists: many short lines (typical of OCR'd notices and
    PDF circulars). Each row then deserves its own small chunk, or one big chunk drowns it."""
    lines = _line_spans(text)
    if len(lines) < 6:
        return False
    lengths = sorted(e - s for s, e in lines)
    return lengths[len(lengths) // 2] <= 110


def chunk_lines(text: str, max_tokens: int = LINE_CHUNK_TOKENS) -> list[ChunkIn]:
    """Groups of whole lines up to ~max_tokens, repeating the previous line for context."""
    spans = _line_spans(text)
    chunks: list[ChunkIn] = []
    i = 0
    while i < len(spans):
        j, total = i, 0
        while j < len(spans) and (j == i or total + count_tokens(text[spans[j][0]:spans[j][1]]) <= max_tokens):
            total += count_tokens(text[spans[j][0]:spans[j][1]])
            j += 1
        s, e = spans[i][0], spans[j - 1][1]
        chunks.append(ChunkIn(text=text[s:e], char_start=s, char_end=e))
        if j >= len(spans):
            break
        i = j - 1 if j - 1 > i else j  # one line of overlap
    return chunks


def chunk_text(text: str, max_tokens: int = MAX_TOKENS, overlap: int = OVERLAP_TOKENS) -> list[ChunkIn]:
    if not text.strip():
        return []
    if is_structured(text):
        return chunk_lines(text)
    spans = _sentence_spans(text)
    if count_tokens(text) <= max_tokens:
        s, e = spans[0][0], spans[-1][1]
        return [ChunkIn(text=text[s:e], char_start=s, char_end=e)]

    units: list[tuple[int, int, int]] = []  # (start, end, tokens)
    for s, e in spans:
        n = count_tokens(text[s:e])
        if n > max_tokens:
            units += [(a, b, count_tokens(text[a:b])) for a, b in _split_long(text, s, e, max_tokens)]
        elif n:
            units.append((s, e, n))

    chunks: list[ChunkIn] = []
    i = 0
    while i < len(units):
        j, total = i, 0
        while j < len(units) and (total + units[j][2] <= max_tokens or j == i):
            total += units[j][2]
            j += 1
        s, e = units[i][0], units[j - 1][1]
        chunks.append(ChunkIn(text=text[s:e], char_start=s, char_end=e))
        if j >= len(units):
            break
        # Step back over trailing sentences until ~overlap tokens are repeated,
        # but always make progress.
        back, k = 0, j
        while k - 1 > i and back + units[k - 1][2] <= overlap:
            k -= 1
            back += units[k][2]
        i = k if k > i else j
    return chunks
