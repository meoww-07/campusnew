"""Subject normalization for Smart Spaces.

"dbms", "Database Management Systems", "DBMS lab", "CS301" -> "DBMS".

Order of resolution for a raw subject:
  1. reject empty / multi-course strings ("DBMS, OS")
  2. alias map (app/subject_aliases.json, path overridable via SUBJECT_ALIASES_PATH)
  3. an existing subject name of this user that it abbreviates or expands
     (reuses repo.same_subject: DBMS ~ DataBase Management Systems)
  4. a tidy display form of the raw text (short single words upper-cased)
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from app import repo

DEFAULT_ALIASES = Path(__file__).resolve().parent.parent / "subject_aliases.json"
_CODE = re.compile(r"\(.*?\)|\b[a-z]{2,4}\s?-?\d{3,4}[a-z]?\b")
_NOISE = {"lab", "labs", "theory", "course", "midsem", "mid", "sem", "endsem", "exam", "exams", "quiz",
          "practical", "tutorial", "class", "classes", "subject", "paper", "the", "of", "and", "&", "for", "in"}
_MULTI = re.compile(r",|/|&|\band\b|;")
# Not courses, or already covered by the built-in category spaces.
_NOT_SUBJECTS = {"none", "null", "general", "misc", "other", "all", "various", "placement", "placements",
                 "internship", "internships", "deadline", "deadlines", "notice", "notices", "resource", "resources",
                 "event", "events", "announcement", "announcements"}


def clean_key(raw: str) -> str:
    """Lowercase, drop course codes in context, punctuation and noise words."""
    s = raw.lower().strip()
    code_only = re.fullmatch(r"[a-z]{2,4}\s?-?\d{3,4}[a-z]?", s)
    if code_only:
        return s.replace(" ", "").replace("-", "")  # 'CS 301' -> 'cs301' (alias maps can use codes)
    s = _CODE.sub(" ", s)
    words = [w for w in re.findall(r"[a-z0-9+#]+", s) if w not in _NOISE]
    return " ".join(words)


@lru_cache(maxsize=8)
def load_aliases(path: str | None = None) -> dict[str, str]:
    """{cleaned alias -> canonical name}, including each canonical name itself."""
    p = Path(path) if path else DEFAULT_ALIASES
    data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    out: dict[str, str] = {}
    for canonical, aliases in data.items():
        if canonical.startswith("_"):
            continue
        for name in [canonical, *aliases]:
            key = clean_key(name)
            if key:
                out[key] = canonical
    return out


def display_name(key: str) -> str:
    if " " not in key and len(key) <= 5 and key.isalpha():
        return key.upper()  # os -> OS, dbms -> DBMS
    return " ".join(w if w.isupper() else w.capitalize() for w in key.split())


def subject_key(name: str) -> str:
    return "subject:" + name.lower()


def normalize(raw: str | None, aliases: dict[str, str], known: Iterable[str] = ()) -> str | None:
    """Canonical display name for a raw subject, or None if it isn't one subject."""
    if not raw or not raw.strip() or _MULTI.search(raw):
        return None
    key = clean_key(raw)
    if not key or key in _NOT_SUBJECTS:
        return None
    if key in aliases:
        return aliases[key]
    no_space = key.replace(" ", "")
    if no_space in aliases:
        return aliases[no_space]
    for name in known:
        if name.lower() == key or repo.same_subject(name, key):
            return name
    if re.fullmatch(r"[a-z]{2,4}\d{3,4}[a-z]?", key):
        return None  # a bare course code nobody mapped: not a useful space name
    return display_name(key)
