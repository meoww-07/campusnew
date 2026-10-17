"""File intake shared by Telegram, the web upload box and Gmail attachments.

Files are validated by magic bytes (never by extension or declared MIME),
stored under FILES_DIR/<user_id>/<sha256>.<ext>, and queued for the worker.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from app import repo
from app.models import IngestResult, SourceType

FileKind = Literal["pdf", "png", "jpg", "txt"]
JOB_KIND: dict[FileKind, str] = {"pdf": "pdf", "png": "image", "jpg": "image"}


class UploadRejected(ValueError):
    """Raised with a user-facing message."""


def detect_kind(data: bytes) -> FileKind | None:
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    head = data[:8192]
    # Text has no control bytes except tab/newline/CR/form feed (rules out zip, exe, ...).
    if not any(b < 0x20 and b not in (0x09, 0x0A, 0x0C, 0x0D) for b in head):
        try:
            head.decode("utf-8")
            return "txt"
        except UnicodeDecodeError as e:
            if e.start >= len(head) - 4:  # multibyte char cut at the boundary
                return "txt"
    return None


def store_file(
    conn: sqlite3.Connection,
    files_dir: Path,
    user_id: int,
    data: bytes,
    *,
    source_type: SourceType,
    filename: str | None,
    observed_at: datetime,
    locator: dict[str, Any] | None = None,
    caption: str | None = None,
    external_id: str | None = None,
    max_bytes: int = 20 * 1024 * 1024,
    allowed: frozenset[FileKind] = frozenset({"pdf", "png", "jpg"}),
) -> IngestResult:
    """Validate, persist and queue a PDF or image. Raises UploadRejected."""
    if len(data) > max_bytes:
        raise UploadRejected(f"File is too large (max {max_bytes // (1024 * 1024)} MB).")
    if not data:
        raise UploadRejected("The file is empty.")
    kind = detect_kind(data)
    if kind is None or kind not in allowed or kind not in JOB_KIND:
        raise UploadRejected("Unsupported file type. I accept PDF, PNG and JPG files.")

    digest = repo.content_hash(data)
    path = files_dir / str(user_id) / f"{digest}.{kind}"
    is_new_file = not path.exists()
    if is_new_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    try:
        result = repo.ingest_source(
            conn, user_id, source_type, digest,
            external_id=external_id,
            title=filename or path.name,
            file_path=str(path),
            job_kind=JOB_KIND[kind],
            job_payload={
                "observed_at": repo.iso(observed_at),
                "locator": {**(locator or {}), "file_name": filename},
                "caption": caption,
            },
        )
    except BaseException:
        if is_new_file:
            path.unlink(missing_ok=True)
        raise
    return result
