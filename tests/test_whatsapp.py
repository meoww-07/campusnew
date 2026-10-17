from datetime import datetime
from pathlib import Path

from app import repo
from app.ingest import whatsapp as wa
from tests.conftest import IST

SAMPLE = """﻿12/10/26, 9:15 pm - Messages and calls are end-to-end encrypted. Tap to learn more.
12/10/26, 9:15 pm - Riya created group "CSE A"
12/10/26, 9:16 pm - Riya: DBMS midsem update:
Postponed to 23 Oct, 2 pm
Venue LH-3
12/10/26, 9:17 PM - Arjun: <Media omitted>
12/10/2026, 21:18 - Priya Nair: 24h format works
13/10/26, 7:05 am - +91 98765 43210: Lab viva on 24 Oct
13/10/26, 7:06 AM - Rohan: This message was deleted
13/10/26, 12:30 a.m. - Sneha: midnight-ish
[14/10/26, 9:15:03 PM] Karthik: iOS format: message
continues here
14/10/26, 10:00 pm - Riya added Ananya
14/10/26, 10:01 pm - Ananya: Thanks: glad to join
"""


def test_parse_formats_and_multiline():
    msgs = wa.parse(SAMPLE)
    assert [m.sender for m in msgs] == ["Riya", "Priya Nair", "+91 98765 43210", "Sneha", "Karthik", "Ananya"]
    assert msgs[0].text == "DBMS midsem update:\nPostponed to 23 Oct, 2 pm\nVenue LH-3"
    assert msgs[0].timestamp == datetime(2026, 10, 12, 21, 16, tzinfo=IST)
    assert msgs[0].line_no == 3
    assert msgs[1].timestamp == datetime(2026, 10, 12, 21, 18, tzinfo=IST)  # 24h, 4-digit year
    assert msgs[2].timestamp.hour == 7  # narrow nbsp + lowercase am
    assert msgs[3].timestamp.hour == 0  # 12:30 a.m.
    assert msgs[4].text == "iOS format: message\ncontinues here"
    assert msgs[4].timestamp.second == 3
    assert msgs[5].text == "Thanks: glad to join"  # only first ": " splits sender


def test_dd_mm_order_and_invalid_dates_are_continuations():
    msgs = wa.parse("05/10/26, 9:10 am - A: first\n31/02/26, 9:10 am - B: not a real date\n")
    assert len(msgs) == 1 and msgs[0].timestamp.month == 10 and msgs[0].timestamp.day == 5
    assert "31/02/26" in msgs[0].text


def test_chat_name_from_filename():
    assert wa.chat_name_from_filename("WhatsApp Chat with CSE 3rd Year Sec A.txt") == "CSE 3rd Year Sec A"
    assert wa.chat_name_from_filename("WhatsApp Chat - Hostel Block C.txt") == "Hostel Block C"
    assert wa.chat_name_from_filename("export.txt") == "export"


def test_import_export_stores_segments(conn, users):
    a, _ = users
    res = wa.import_export(conn, a, SAMPLE.encode("utf-8"), "WhatsApp Chat with CSE A.txt")
    assert res.created
    segs = repo.list_segments(conn, a, res.source_id)
    assert len(segs) == 6
    assert segs[0].locator == {"chat_name": "CSE A", "sender": "Riya", "line_no": 3}
    assert segs[0].observed_at == "2026-10-12T15:46:00+00:00"
    assert repo.get_source(conn, a, res.source_id).type == "whatsapp"
    again = wa.import_export(conn, a, SAMPLE.encode("utf-8"), "copy.txt")
    assert not again.created


def test_not_a_chat_export(conn, users):
    a, _ = users
    assert wa.import_export(conn, a, b"just some notes\nno timestamps", "notes.txt") is None


def test_seed_export_parses():
    path = Path(__file__).resolve().parent.parent / "seed" / "WhatsApp Chat with CSE 3rd Year Sec A.txt"
    msgs = wa.parse(path.read_text(encoding="utf-8"))
    assert 75 <= len(msgs) <= 95
    assert not any("Media omitted" in m.text or "deleted" in m.text for m in msgs)
    multi = [m for m in msgs if "\n" in m.text]
    assert len(multi) >= 4
    assert any("postponed to 23 October" in m.text for m in msgs)
