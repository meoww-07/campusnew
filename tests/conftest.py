from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import repo
from app.db import EMBED_DIM, init_db
from app.models import SegmentIn

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def conn(tmp_path) -> Iterator[sqlite3.Connection]:
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def users(conn) -> tuple[int, int]:
    a = repo.upsert_user(conn, 1001, "Alice").id
    b = repo.upsert_user(conn, 2002, "Bob").id
    return a, b


def unit_vec(*hot: int) -> list[float]:
    """A normalized 384-d vector with weight on the given dimensions."""
    v = [0.0] * EMBED_DIM
    for i in hot:
        v[i] = 1.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def seg(text: str, **locator) -> SegmentIn:
    return SegmentIn(text=text, locator=locator, observed_at=datetime(2026, 10, 12, 21, 15, tzinfo=IST))


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)
