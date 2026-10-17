import base64
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from app import repo
from app.config import Settings
from app.ingest import gmail
from app.organize import worker
from tests.fakes import FakeEmbedder, make_pdf


def enc(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


PLAIN = """Dear students,

The CN lab viva is rescheduled to 25 October, 10 am.

Regards,
Prof. Menon
--
Prof. A. Menon | Dept. of CSE
DISCLAIMER: This email is confidential.

On Mon, 12 Oct 2026 at 09:00, Riya Sharma <riya@college.edu> wrote:
> When is the viva?
"""
HTML = "<html><body><p>Dear students,</p><p>The CN lab viva is <b>rescheduled</b>.</p>" \
       "<div class='gmail_quote'>On Mon ... wrote: old stuff</div></body></html>"
PDF = make_pdf(["Viva schedule: Roll 1-20 at 10 am, Roll 21-40 at 11:30 am, Roll 41-60 at 2 pm in Lab 4."])


def message(msg_id="m1", body=None, html=HTML, attachments=True, subject="CN viva rescheduled"):
    parts = [{"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": enc(body if body is not None else PLAIN)}},
        {"mimeType": "text/html", "body": {"data": enc(html)}},
    ]}]
    if attachments:
        parts += [
            {"mimeType": "application/pdf", "filename": "viva/schedule.pdf",
             "body": {"attachmentId": "att1", "size": len(PDF)}},
            {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "filename": "notes.docx", "body": {"attachmentId": "att2", "size": 10}},
        ]
    return {"id": msg_id, "threadId": "t1", "internalDate": str(int(datetime(2026, 10, 13, 4, 30,
                                                                              tzinfo=timezone.utc).timestamp() * 1000)),
            "payload": {"mimeType": "multipart/mixed", "headers": [
                {"name": "Subject", "value": subject}, {"name": "From", "value": "Prof. Menon <menon@college.edu>"}],
                "parts": parts}}


class FakeClient:
    def __init__(self, msgs):
        self.msgs = {m["id"]: m for m in msgs}
        self.queries: list[str] = []
        self.fetched: list[str] = []

    def list_ids(self, query, limit):
        self.queries.append(query)
        return list(self.msgs)[:limit]

    def get(self, msg_id):
        self.fetched.append(msg_id)
        return self.msgs[msg_id]

    def attachment(self, msg_id, att_id):
        return PDF if att_id == "att1" else b"PK\x03\x04docx"


@pytest.fixture
def settings(tmp_path):
    return Settings(files_dir=tmp_path / "files", google_client_id="cid.apps.googleusercontent.com",
                    google_client_secret="secret", app_base_url="http://localhost:8000")


# ---------- parsing ----------

def test_parse_prefers_plain_and_cleans():
    e = gmail.parse_message(message())
    assert e.subject == "CN viva rescheduled" and e.sender.startswith("Prof. Menon")
    assert e.body == "Dear students,\n\nThe CN lab viva is rescheduled to 25 October, 10 am.\n\nRegards,\nProf. Menon"
    assert e.date == datetime(2026, 10, 13, 4, 30, tzinfo=timezone.utc)
    assert [a.filename for a in e.attachments] == ["viva/schedule.pdf", "notes.docx"]


def test_parse_html_fallback_strips_quotes():
    e = gmail.parse_message(message(body="", attachments=False))
    assert "rescheduled" in e.body and "old stuff" not in e.body and "<b>" not in e.body


@pytest.mark.parametrize("raw,expected", [
    ("Hi\nSee you\n\nSent from my iPhone", "Hi\nSee you"),
    ("Update below\n\n-----Original Message-----\nFrom: x", "Update below"),
    ("Fee due 31 Oct\n________________\nFrom: Accounts\nSent: Monday", "Fee due 31 Oct"),
    ("Text\n> quoted line\nmore", "Text\nmore"),
    ("Body\n\nThe information contained in this e-mail is confidential and intended for", "Body"),
])
def test_clean_body(raw, expected):
    assert gmail.clean_body(raw) == expected


def test_build_query():
    assert gmail.build_query("newer_than:60d", None) == "newer_than:60d"
    q = gmail.build_query("newer_than:60d", "2026-10-13T00:00:00+00:00")
    assert q == f"newer_than:60d after:{int(datetime(2026, 10, 13, tzinfo=timezone.utc).timestamp()) - 300}"


# ---------- sync + attachments + delete ----------

def test_sync_stores_skips_and_processes_attachments(conn, users, settings):
    a, _ = users
    client = FakeClient([message("m1"), message("m2", body="Fee reminder: pay by 31 Oct", attachments=False,
                                                  subject="Fee reminder")])
    res = gmail.sync_user(conn, settings, a, client)
    assert (res.listed, res.stored, res.skipped) == (2, 2, 0)
    assert client.queries == ["newer_than:60d"]
    src = next(s for s in repo.list_sources(conn, a) if s.external_id == "m1")
    folder = Path(src.file_path)
    assert src.type == "gmail" and folder.is_dir()
    assert [p.name for p in folder.iterdir()] == ["schedule.pdf"]  # docx rejected by magic bytes

    worker.drain(conn, FakeEmbedder())
    segs = repo.list_segments(conn, a, src.id)
    assert segs[0].locator == {"gmail_id": "m1", "thread_id": "t1", "from": "Prof. Menon <menon@college.edu>",
                               "subject": "CN viva rescheduled"}
    assert segs[0].text.startswith("Subject: CN viva rescheduled\nFrom: Prof. Menon")
    att = segs[1]
    assert att.locator["attachment"] == "schedule.pdf" and att.locator["page"] == 1 and "Roll 21-40" in att.text
    assert repo.fts_search(conn, a, '"roll"')

    # Second sync: incremental query, already-stored ids are not fetched again.
    client2 = FakeClient([message("m1"), message("m3", subject="New", body="Quiz on Friday", attachments=False)])
    res2 = gmail.sync_user(conn, settings, a, client2)
    assert (res2.stored, res2.skipped) == (1, 1) and client2.fetched == ["m3"]
    assert "after:" in client2.queries[0]

    assert repo.delete_source(conn, a, src.id)
    assert not folder.exists()


def test_duplicate_content_cleans_up_files(conn, users, settings):
    a, _ = users
    gmail.sync_user(conn, settings, a, FakeClient([message("m1")]))
    gmail.sync_user(conn, settings, a, FakeClient([message("m9")]))  # same content, new id
    assert not (settings.files_dir / str(a) / "gmail_m9").exists()
    assert len(repo.list_sources(conn, a)) == 1


# ---------- OAuth ----------

def test_start_connect_url(settings):
    store = gmail.OAuthStateStore()
    url = gmail.start_connect(settings, 7, store)
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["cid.apps.googleusercontent.com"]
    assert q["redirect_uri"] == ["http://localhost:8000/gmail/callback"]
    assert q["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert store.pop(q["state"][0]).user_id == 7
    assert store.pop(q["state"][0]) is None  # single use


def test_start_connect_requires_config(tmp_path):
    with pytest.raises(gmail.OAuthError):
        gmail.start_connect(Settings(files_dir=tmp_path), 1)


def test_finish_connect_stores_refresh_token(conn, users, settings, monkeypatch):
    a, _ = users
    store = gmail.OAuthStateStore()
    store.put("st", a, "verifier")
    seen = {}

    class FakeFlow:
        credentials = SimpleNamespace(refresh_token="1//refresh")
        code_verifier = None

        def fetch_token(self, code):
            seen["code"], seen["verifier"] = code, self.code_verifier

    monkeypatch.setattr(gmail, "_flow", lambda s, state=None: FakeFlow())
    assert gmail.finish_connect(conn, settings, "st", "authcode", store) == a
    assert seen == {"code": "authcode", "verifier": "verifier"}
    assert repo.get_user(conn, a).gmail_refresh_token == "1//refresh"
    with pytest.raises(gmail.OAuthError, match="expired"):
        gmail.finish_connect(conn, settings, "st", "authcode", store)
