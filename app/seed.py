"""Load the sample data for a Telegram user.

    python -m app.seed <telegram_chat_id> [--no-process] [--no-llm] [--no-demo]

1. Imports the WhatsApp class-group export and the two circular PDFs and runs
   the worker over them (embeddings + LLM extraction).
2. Adds the demo set (no LLM needed, dated relative to *today* so the demo
   always looks current): ~12 events this month incl. one due within 24h, a
   real conflict and a superseded date, placement mails for a custom
   "Placement Cell" space, an old resource for Rediscover, pinned spaces.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import repo
from app.config import PROJECT_ROOT, Settings, get_settings
from app.db import init_db
from app.ingest import whatsapp
from app.ingest.upload import store_file
from app.llm import LLM, make_llm
from app.models import EventIn, SegmentIn, SourceType, SpaceRules, User
from app.organize import spaces, worker
from app.organize.indexer import Embedder, FastEmbedder, index_segments

SEED_DIR = PROJECT_ROOT / "seed"
IST = ZoneInfo("Asia/Kolkata")
CHAT_FILE = "WhatsApp Chat with CSE 3rd Year Sec A.txt"
PDFS = {  # file -> date the circular was issued (used as observed_at)
    "Midsem_Schedule_Circular.pdf": datetime(2026, 9, 25, 15, 31, tzinfo=IST),
    "Accounts_and_Library_Circular.pdf": datetime(2026, 10, 5, 11, 0, tzinfo=IST),
}
DEMO_VERSION = "demo-v1"
DEMO_CHAT = "CSE 3rd Year Sec A"


def load_seed(conn: sqlite3.Connection, settings: Settings, chat_id: int, name: str = "Seed User") -> int:
    """Queue the WhatsApp export + circular PDFs for the user. Returns the user id."""
    user = repo.upsert_user(conn, chat_id, name)
    chat = SEED_DIR / CHAT_FILE
    res = whatsapp.import_export(conn, user.id, chat.read_bytes(), chat.name)
    print(f"WhatsApp export: {'queued' if res and res.created else 'already loaded'}")
    for fname, issued in PDFS.items():
        res = store_file(conn, settings.files_dir, user.id, (SEED_DIR / fname).read_bytes(),
                         source_type="upload", filename=fname, observed_at=issued)
        print(f"{fname}: {'queued' if res.created else 'already loaded'}")
    return user.id


def process(conn: sqlite3.Connection, embedder: Embedder, llm: LLM | None) -> None:
    print(f"Processing jobs (LLM extraction {'on' if llm else 'off'}) ...")
    n = worker.drain(conn, embedder, llm)
    failed = conn.execute("SELECT id, kind, last_error FROM jobs WHERE status = 'failed'").fetchall()
    print(f"{n} job runs; {len(failed)} failed")
    for f in failed:
        print(f"  job {f['id']} ({f['kind']}): {f['last_error']}")


# ---------- demo set ----------

@dataclass
class DemoEvent:
    title: str
    type: str
    subject: str | None
    due: datetime
    all_day: bool = False


@dataclass
class DemoItem:
    key: str
    source: SourceType
    text: str
    observed: datetime
    added: datetime
    locator: dict
    category: str
    importance: str
    subject: str | None = None
    summary: str | None = None
    title: str | None = None
    events: list[DemoEvent] = field(default_factory=list)


def _demo_items(now: datetime) -> list[DemoItem]:
    tz = IST
    today = now.astimezone(tz).date()

    def at(days: int, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(today + timedelta(days=days), time(hour, minute), tz)

    def wa(sender: str) -> dict:
        return {"chat_name": DEMO_CHAT, "sender": sender}

    def mail(frm: str, subject: str, gid: str) -> dict:
        return {"gmail_id": gid, "thread_id": gid, "from": frm, "subject": subject}

    soon = (now + timedelta(hours=20)).astimezone(tz).replace(minute=0, second=0, microsecond=0)
    old = now - timedelta(days=12)
    d = lambda n: (today + timedelta(days=n)).strftime("%a %d %b")  # noqa: E731
    return [
        DemoItem("cn-record", "whatsapp", f"Reminder: CN lab record must be submitted by {soon.strftime('%I %p').lstrip('0')} "
                 f"{'tomorrow' if soon.date() != today else 'today'} in Lab 4. Get all 8 experiments signed.",
                 now - timedelta(hours=3), now - timedelta(hours=3), wa("Riya Sharma"), "deadline", "high", "CN",
                 "CN lab record submission, Lab 4, all 8 experiments signed",
                 events=[DemoEvent("CN lab record submission", "deadline", "CN", soon)]),
        DemoItem("cd-quiz3-a", "whatsapp", f"Compiler Design Quiz 3 will be on {d(6)}, 9 am, syllabus: syntax-directed translation.",
                 now - timedelta(days=2), now - timedelta(days=2), wa("Riya Sharma"), "exam", "high", "Compiler Design",
                 "CD Quiz 3: syntax-directed translation", events=[DemoEvent("CD Quiz 3", "exam", "Compiler Design", at(6, 9))]),
        DemoItem("cd-quiz3-b", "whatsapp", f"cd quiz 3 is on {d(7)} 9am na? ma'am said something in class",
                 now - timedelta(days=1), now - timedelta(days=1), wa("Arjun Mehta"), "exam", "medium", "Compiler Design",
                 "CD Quiz 3 date mentioned differently", events=[DemoEvent("CD Quiz 3", "exam", "Compiler Design", at(7, 9))]),
        DemoItem("se-review-old", "gmail", f"Dear students, the SE project review will be held on {d(9)} at 11 am in Seminar Hall 2.",
                 now - timedelta(days=6), now - timedelta(days=6),
                 mail("Dr. Rao <rao.se@college.edu.in>", "SE project review", "demo-se-1"), "event", "high", "SE",
                 "SE project review in Seminar Hall 2", events=[DemoEvent("SE project review", "event", "SE", at(9, 11))]),
        DemoItem("se-review-new", "gmail", f"UPDATE: the SE project review is rescheduled to {d(11)}, 2 pm, same venue.",
                 now - timedelta(days=1, hours=2), now - timedelta(days=1, hours=2),
                 mail("Dr. Rao <rao.se@college.edu.in>", "Re: SE project review", "demo-se-2"), "event", "high", "SE",
                 "SE project review rescheduled", events=[DemoEvent("SE project review", "event", "SE", at(11, 14))]),
        DemoItem("tcs", "gmail", f"TCS Digital campus drive: registration closes {d(3)} 11:59 pm on the placement portal. "
                 f"Online test on {d(8)} at 10 am. Eligibility: 7 CGPA, no active backlogs.",
                 now - timedelta(hours=5), now - timedelta(hours=5),
                 mail("Training & Placement Cell <tpo@college.edu.in>", "TCS Digital campus drive", "demo-tcs"),
                 "placement", "high", None, "TCS Digital drive: register by deadline, online test, 7 CGPA",
                 events=[DemoEvent("TCS Digital registration", "deadline", None, at(3, 23, 59)),
                         DemoEvent("TCS Digital online test", "exam", None, at(8, 10))]),
        DemoItem("infosys", "gmail", f"Infosys pre-placement talk for the summer internship on {d(5)} at 2 pm in the auditorium. "
                 "Attendance is mandatory for applicants.", now - timedelta(days=3), now - timedelta(days=3),
                 mail("Training & Placement Cell <tpo@college.edu.in>", "Infosys internship PPT", "demo-infy"),
                 "placement", "medium", None, "Infosys internship pre-placement talk, auditorium",
                 events=[DemoEvent("Infosys internship talk", "event", None, at(5, 14))]),
        DemoItem("amazon", "whatsapp", "Amazon is hiring SDE interns off campus, apply on their careers page before the 30th. Referral link in the group.",
                 now - timedelta(days=4), now - timedelta(days=4), wa("Karthik R"), "placement", "medium", None,
                 "Amazon SDE intern hiring, off campus"),
        DemoItem("dbms-asg3", "whatsapp", f"DBMS assignment 3 (transactions, concurrency control) is due {d(4)} 11:59 pm on Moodle.",
                 now - timedelta(days=2, hours=4), now - timedelta(days=2, hours=4), wa("Prof. Kulkarni"), "deadline", "high",
                 "DBMS", "DBMS assignment 3 on transactions due on Moodle",
                 events=[DemoEvent("DBMS Assignment 3", "deadline", "DBMS", at(4, 23, 59))]),
        DemoItem("dbms-lab", "whatsapp", f"DBMS lab exam on {d(10)} 2 pm, SQL queries + one PL/SQL program.",
                 now - timedelta(days=1, hours=6), now - timedelta(days=1, hours=6), wa("Riya Sharma"), "exam", "high", "DBMS",
                 "DBMS lab exam: SQL + PL/SQL", events=[DemoEvent("DBMS lab exam", "exam", "DBMS", at(10, 14))]),
        DemoItem("os-quiz2", "whatsapp", f"OS quiz 2 on {d(2)} 10 am, topics: paging, segmentation, page replacement.",
                 now - timedelta(days=1), now - timedelta(days=1), wa("Riya Sharma"), "exam", "high", "OS",
                 "OS quiz 2 on memory management", events=[DemoEvent("OS Quiz 2", "exam", "OS", at(2, 10))]),
        DemoItem("hackathon", "telegram", f"Smart India Hackathon internal round demo day on {d(13)}, 9 am in the CSE block.",
                 now - timedelta(hours=8), now - timedelta(hours=8), {"chat_id": 0, "message_id": 0, "forward_sender": "GDG IIIT"},
                 "event", "medium", None, "SIH internal round demo day",
                 events=[DemoEvent("SIH internal demo day", "event", None, at(13, 9))]),
        DemoItem("library-closed", "whatsapp", f"Library will be closed on {d(1)} for stock verification.",
                 now - timedelta(hours=10), now - timedelta(hours=10), wa("Sneha Iyer"), "notice", "medium", None,
                 "Library closed for stock verification"),
        DemoItem("dbms-pyq", "upload", "DBMS previous year question papers 2023-2025 with solutions: normalization, SQL, "
                 "transactions, indexing (B+ trees), ER diagrams. Prepared by the 2025 batch.",
                 old, old, {"file_name": "DBMS_PYQ_Solutions.pdf", "page": 1}, "resource", "high", "DBMS",
                 "DBMS previous year papers with solutions", title="DBMS_PYQ_Solutions.pdf"),
        DemoItem("os-notes", "upload", "OS unit 3 handwritten notes: deadlock detection, banker's algorithm worked examples, "
                 "resource allocation graphs.", now - timedelta(days=9), now - timedelta(days=9),
                 {"file_name": "OS_Unit3_Notes.pdf", "page": 1}, "resource", "high", "OS",
                 "OS unit 3 notes on deadlocks and banker's algorithm", title="OS_Unit3_Notes.pdf"),
    ]


def load_demo(conn: sqlite3.Connection, settings: Settings, user: User, embedder: Embedder,
              now: datetime | None = None) -> dict[str, int]:
    """Insert the demo set for a user (idempotent per item). Returns counts."""
    now = now or datetime.now(timezone.utc)
    added_segments: list[int] = []
    n_events = 0
    for item in _demo_items(now):
        res = repo.ingest_source(conn, user.id, item.source, repo.content_hash(f"{DEMO_VERSION}:{item.key}"),
                                 [SegmentIn(text=item.text, observed_at=item.observed, locator=item.locator)],
                                 title=item.title or item.text[:60],
                                 external_id=item.locator.get("gmail_id"), job_kind="demo")
        if not res.created:
            continue
        conn.execute("UPDATE sources SET created_at = ? WHERE id = ?", (repo.iso(item.added), res.source_id))
        conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (res.job_id,))  # processed right here
        [seg] = repo.list_segments(conn, user.id, res.source_id)
        index_segments(conn, user.id, [seg], embedder)
        events = [EventIn(segment_id=seg.id, title=e.title, type=e.type, subject=e.subject, due_at=e.due,
                          all_day=e.all_day, confidence=0.9) for e in item.events]
        n_events += len(repo.apply_extraction(conn, user.id, seg.id, summary=item.summary, category=item.category,
                                              subject=item.subject, importance=item.importance, events=events,
                                              now=now))
        added_segments.append(seg.id)

    spaces.backfill_user(conn, user.id, embedder)
    try:
        cell = repo.create_space(conn, user.id, "Placement Cell", "custom", icon="🎯", color="#E7B06E",
                                 rules=SpaceRules(senders=["@college.edu.in"],
                                                  any_keywords=["placement", "drive", "internship", "hiring", "ppt"]))
    except repo.SpaceNameTaken:
        cell = next(s for s in repo.list_spaces(conn, user.id) if s.name == "Placement Cell")
    spaces.backfill_space(conn, user.id, cell.id, embedder)
    for space in repo.list_spaces(conn, user.id):
        if space.name in ("DBMS", "Placement Cell"):
            repo.update_space(conn, user.id, space.id, pinned=True)
    return {"segments": len(added_segments), "events": n_events}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("telegram_chat_id", type=int)
    ap.add_argument("--no-process", action="store_true", help="only queue; let the running app's worker process")
    ap.add_argument("--no-llm", action="store_true", help="skip LLM extraction")
    ap.add_argument("--no-demo", action="store_true", help="skip the dated demo set")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    settings = get_settings()
    conn = init_db(settings.db_path)
    user_id = load_seed(conn, settings, args.telegram_chat_id)
    embedder = FastEmbedder()
    if not args.no_process:
        process(conn, embedder, None if args.no_llm else make_llm(settings))
    if not args.no_demo:
        print("Demo set:", load_demo(conn, settings, repo.get_user(conn, user_id), embedder))
    counts = {t: conn.execute(f"SELECT count(*) FROM {t} WHERE user_id = ?", (user_id,)).fetchone()[0]
              for t in ("sources", "segments", "chunks", "events", "spaces")}
    print("Loaded for user", user_id, counts)


if __name__ == "__main__":
    main()
