"""Smart Spaces: automatic and rule-based collections of segments.

Three ways a segment lands in a space (one segment may be in many):
  auto (subject)   normalized subject of the segment or of its events
                   (DBMS / Database Management Systems / CS301 -> "DBMS").
                   The space is created once a subject has >= 2 items.
  auto (category)  built-in Exams, Deadlines, Notices, Resources, Placements,
                   from the LLM category and the segment's event types.
  rule             custom spaces' rules_json (keywords, source types, senders,
                   categories, date range, optional semantic "similar_to").
Exclusions (user removed an item) are always respected; see repo.add_space_items.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app import repo
from app.config import get_settings
from app.models import SegmentContext, Space, SpaceRules
from app.organize import subjects
from app.organize.indexer import Embedder

log = logging.getLogger(__name__)

MIN_ITEMS_FOR_AUTO_SPACE = 2
SUBJECT_COLORS = ["#3d5afe", "#00897b", "#8e24aa", "#f4511e", "#6d4c41", "#0288d1", "#c0ca33", "#d81b60"]


@dataclass(frozen=True)
class CategorySpace:
    key: str
    name: str
    icon: str
    color: str
    categories: frozenset[str]
    event_types: frozenset[str] = frozenset()
    default_rules: SpaceRules = field(default_factory=SpaceRules)


CATEGORY_SPACES: tuple[CategorySpace, ...] = (
    CategorySpace("category:exam", "Exams", "📝", "#c62828", frozenset({"exam"}), frozenset({"exam"})),
    CategorySpace("category:deadline", "Deadlines", "⏰", "#ef6c00", frozenset({"deadline"}), frozenset({"deadline"})),
    CategorySpace("category:notice", "Notices", "📢", "#5e35b1", frozenset({"notice"})),
    CategorySpace("category:resource", "Resources", "📚", "#2e7d32", frozenset({"resource"})),
    CategorySpace("category:placement", "Placements", "💼", "#00838f", frozenset({"placement"}),
                  default_rules=SpaceRules(any_keywords=[
                      "placement", "placements", "internship", "recruitment", "recruiter", "campus drive",
                      "hiring", "job offer", "ppo", "aptitude test", "off campus", "on campus drive"])),
)


def aliases() -> dict[str, str]:
    return subjects.load_aliases(get_settings().subject_aliases_path or None)


# ---------- rule matching (pure) ----------

def _has_word(haystack: str, needle: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(needle.lower())}(?![\w])", haystack) is not None


def senders_of(ctx: SegmentContext) -> list[str]:
    loc = ctx.locator
    return [str(v).lower() for v in (loc.get("sender"), loc.get("from"), loc.get("forward_sender")) if v]


def _sender_matches(pattern: str, senders: list[str]) -> bool:
    p = pattern.lower().strip()
    domain = p[1:] if p.startswith("@") else (p if "." in p and "@" not in p and " " not in p else None)
    for s in senders:
        if domain:
            emails = re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", s)
            if any(e == domain or e.endswith("." + domain) for e in emails):
                return True
        elif p in s:
            return True
    return False


def match_rules(rules: SpaceRules, ctx: SegmentContext, distance: float | None = None,
                default_max_distance: float = 0.3) -> tuple[bool, float]:
    """(matches, score). Every condition that is set must hold; empty rules never match.

    `distance` is the segment's closest cosine distance to `similar_to` (None if
    not computed / no vectors).
    """
    if rules.is_empty():
        return False, 0.0
    text = " ".join(t for t in (ctx.text, ctx.summary, ctx.source_title) if t).lower()
    if rules.any_keywords and not any(_has_word(text, k) for k in rules.any_keywords):
        return False, 0.0
    if rules.all_keywords and not all(_has_word(text, k) for k in rules.all_keywords):
        return False, 0.0
    if rules.source_types and ctx.source_type not in rules.source_types:
        return False, 0.0
    if rules.senders and not any(_sender_matches(p, senders_of(ctx)) for p in rules.senders):
        return False, 0.0
    if rules.categories and ctx.category not in rules.categories \
            and not set(rules.categories) & set(ctx.event_types):
        return False, 0.0
    observed = datetime.fromisoformat(ctx.observed_at)
    if rules.date_from and observed < _aware(rules.date_from):
        return False, 0.0
    if rules.date_to and observed > _aware(rules.date_to):
        return False, 0.0
    score = 1.0
    if rules.similar_to:
        limit = rules.similar_max_distance or default_max_distance
        if distance is None or distance > limit:
            return False, 0.0
        score = round(1.0 - distance, 4)
    return True, score


def _aware(dt: datetime) -> datetime:
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------- assignment ----------

def ensure_category_spaces(conn: sqlite3.Connection, user_id: int) -> None:
    """Create the built-in category spaces once (a user-deleted one is not re-created)."""
    for cs in CATEGORY_SPACES:
        if repo.auto_key_exists(conn, user_id, cs.key):
            continue
        try:
            repo.create_space(conn, user_id, cs.name, "auto", auto_key=cs.key, icon=cs.icon, color=cs.color,
                              rules=cs.default_rules)
        except repo.SpaceNameTaken:  # user already has a custom space with that name
            log.info("user %s already has a space named %s; skipping built-in", user_id, cs.name)


def _subject_names(ctx: SegmentContext, alias_map: dict[str, str], known: list[str]) -> dict[str, str]:
    """{subject_key: display name} for a segment and its events."""
    out: dict[str, str] = {}
    for raw in [ctx.subject, *ctx.event_subjects]:
        name = subjects.normalize(raw, alias_map, known)
        if name:
            out[subjects.subject_key(name)] = name
            if name not in known:
                known.append(name)
    return out


class _VectorCache:
    def __init__(self, embedder: Embedder | None) -> None:
        self.embedder = embedder
        self._q: dict[str, list[float]] = {}

    def query(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        if text not in self._q:
            self._q[text] = self.embedder.embed_query(text)
        return self._q[text]


def _rule_items(conn: sqlite3.Connection, user_id: int, space: Space, contexts: Sequence[SegmentContext],
                vectors: _VectorCache, max_distance: float) -> list[tuple[int, str, float]]:
    qv = vectors.query(space.rules.similar_to) if space.rules.similar_to else None
    items = []
    for ctx in contexts:
        dist = repo.min_segment_distance(conn, user_id, ctx.segment_id, qv) if qv is not None else None
        ok, score = match_rules(space.rules, ctx, dist, max_distance)
        if ok:
            items.append((ctx.segment_id, "rule", score))
    return items


def assign_segments(conn: sqlite3.Connection, user_id: int, segment_ids: Sequence[int],
                    embedder: Embedder | None = None) -> dict[int, int]:
    """Put segments into subject, category and rule spaces. Idempotent.

    Returns {space_id: items newly added}.
    """
    if not segment_ids:
        return {}
    settings = get_settings()
    ensure_category_spaces(conn, user_id)
    contexts = repo.segment_contexts(conn, user_id, list(segment_ids))
    alias_map = aliases()
    known = repo.known_subject_names(conn, user_id)
    added: dict[int, int] = {}

    def add(space_id: int, items: list[tuple[int, str, float]]) -> None:
        n = repo.add_space_items(conn, user_id, space_id, items)
        if n:
            added[space_id] = added.get(space_id, 0) + n

    # Subject spaces
    by_key: dict[str, tuple[str, list[int]]] = {}
    for ctx in contexts:
        names = _subject_names(ctx, alias_map, known)
        repo.set_segment_subjects(conn, user_id, ctx.segment_id, list(names.items()))
        for key, name in names.items():
            by_key.setdefault(key, (name, []))[1].append(ctx.segment_id)
    for key, (name, seg_ids) in by_key.items():
        space = repo.get_space_by_key(conn, user_id, key)
        if space is None:
            if repo.auto_key_exists(conn, user_id, key):
                continue  # the user deleted this subject space
            members = repo.segments_with_subject(conn, user_id, key)
            if len(members) < MIN_ITEMS_FOR_AUTO_SPACE:
                continue  # wait for a second item; no one-item spaces
            n_subject = sum(1 for s in repo.list_spaces(conn, user_id) if (s.auto_key or "").startswith("subject:"))
            try:
                space = repo.create_space(conn, user_id, name, "auto", auto_key=key, icon="📘",
                                          color=SUBJECT_COLORS[n_subject % len(SUBJECT_COLORS)])
            except repo.SpaceNameTaken:
                continue
            seg_ids = members  # include earlier items with this subject
        add(space.id, [(s, "auto", 1.0) for s in seg_ids])

    # Category spaces
    for cs in CATEGORY_SPACES:
        space = repo.get_space_by_key(conn, user_id, cs.key)
        if space is None:
            continue
        members = [c.segment_id for c in contexts
                   if (c.category in cs.categories) or (set(c.event_types) & cs.event_types)]
        add(space.id, [(s, "auto", 1.0) for s in members])

    # Rule spaces (custom + built-ins with default rules, e.g. Placements keywords)
    vectors = _VectorCache(embedder)
    for space in repo.list_rule_spaces(conn, user_id):
        add(space.id, _rule_items(conn, user_id, space, contexts, vectors, settings.space_similarity_max_distance))
    return added


def preview_rules(conn: sqlite3.Connection, user_id: int, rules: SpaceRules, embedder: Embedder | None = None,
                  sample: int = 5) -> dict:
    """How many of the user's segments a rule set matches, with a few examples (live preview)."""
    contexts = repo.segment_contexts(conn, user_id)
    fake = Space(id=0, user_id=user_id, name="preview", kind="custom", rules=rules, created_at="")
    items = _rule_items(conn, user_id, fake, contexts, _VectorCache(embedder),
                        get_settings().space_similarity_max_distance)
    by_id = {c.segment_id: c for c in contexts}
    examples = sorted(items, key=lambda i: by_id[i[0]].observed_at, reverse=True)[:sample]
    return {"count": len(items), "examples": [
        {"segment_id": seg, "text": by_id[seg].text[:200], "source_type": by_id[seg].source_type,
         "observed_at": by_id[seg].observed_at} for seg, _, _ in examples]}


def _event_belongs(space: Space, event: dict) -> bool:
    """Subject spaces only show their own subject's events; other spaces show any."""
    if not (space.auto_key or "").startswith("subject:"):
        return True
    name = subjects.normalize(repo.effective_subject(event.get("subject"), event["title"]), aliases(),
                              known=[space.name])
    return name is not None and subjects.subject_key(name) == space.auto_key


def list_overview(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    return repo.list_spaces_overview(conn, user_id, event_filter=_event_belongs)


# ---------- backfill (worker jobs) ----------

def backfill_space(conn: sqlite3.Connection, user_id: int, space_id: int, embedder: Embedder | None = None) -> int:
    """Re-evaluate one space's rules over all of the user's segments (after create / rules change)."""
    space = repo.get_space(conn, user_id, space_id)
    if space is None:
        return 0
    repo.clear_rule_items(conn, user_id, space_id)  # rules may have narrowed; user-added items stay
    if space.rules.is_empty():
        return 0
    contexts = repo.segment_contexts(conn, user_id)
    items = _rule_items(conn, user_id, space, contexts, _VectorCache(embedder),
                        get_settings().space_similarity_max_distance)
    return repo.add_space_items(conn, user_id, space_id, items)


def backfill_user(conn: sqlite3.Connection, user_id: int, embedder: Embedder | None = None,
                  batch: int = 500) -> int:
    """One-time: category spaces + auto/rule assignment for everything already processed."""
    ensure_category_spaces(conn, user_id)
    ids = [c.segment_id for c in repo.segment_contexts(conn, user_id)]
    total = 0
    for i in range(0, len(ids), batch):
        total += sum(assign_segments(conn, user_id, ids[i:i + batch], embedder).values())
    return total
