from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import repo
from app.config import Settings
from app.db import init_db
from app.models import EventIn, SegmentIn
from app.organize import worker
from app.web import routes
from tests.conftest import IST
from tests.fakes import FakeEmbedder, FakeLLM, make_pdf

H = {"X-CampusMind": "1"}


@pytest.fixture
def env(tmp_path):
    from app import main
    settings = Settings(db_path=tmp_path / "web.db", files_dir=tmp_path / "files", session_secret="test-secret",
                        app_base_url="http://testserver")
    conn = init_db(settings.db_path)
    main.app.dependency_overrides[routes.settings_dep] = lambda: settings
    main.app.state.embedder = FakeEmbedder()
    main.app.state.llm = FakeLLM("DBMS midsem is on 23 Oct [1].")
    main.app.state.telegram = None
    alice = repo.upsert_user(conn, 1001, "Alice").id
    bob = repo.upsert_user(conn, 2002, "Bob").id
    yield {"app": main.app, "conn": conn, "settings": settings, "alice": alice, "bob": bob}
    main.app.dependency_overrides.clear()
    conn.close()


def login(env, user_id) -> TestClient:
    client = TestClient(env["app"])
    token = repo.create_login_token(env["conn"], user_id)
    r = client.get(f"/login?token={token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    return client


def test_index_and_login_flow(env):
    client = TestClient(env["app"])
    assert "CampusMind" in client.get("/").text
    assert client.get("/api/me").status_code == 401
    token = repo.create_login_token(env["conn"], env["alice"])
    assert client.get(f"/login?token={token}", follow_redirects=False).status_code == 303
    assert client.get("/api/me").json()["name"] == "Alice"
    # One-time token
    assert TestClient(env["app"]).get(f"/login?token={token}").status_code == 400


def test_session_cookie_cannot_be_forged(env):
    good = routes.make_session(env["alice"], "test-secret")
    assert routes.read_session(good, "test-secret") == env["alice"]
    assert routes.read_session(good, "other-secret") is None
    forged = routes.make_session(env["bob"], "attacker-guess")
    client = TestClient(env["app"], cookies={routes.COOKIE: forged})
    assert client.get("/api/me").status_code == 401
    expired = routes.make_session(env["alice"], "test-secret", ttl=timedelta(seconds=-5))
    assert routes.read_session(expired, "test-secret") is None


def test_csrf_header_required(env):
    client = login(env, env["alice"])
    assert client.post("/api/ask", json={"question": "x"}).status_code == 403
    assert client.post("/api/ask", json={"question": "x"}, headers=H).status_code == 200


def test_upload_validation(env):
    client = login(env, env["alice"])
    r = client.post("/api/upload", files={"file": ("circ.pdf", make_pdf(["Fee due 31 Oct"]), "application/pdf")}, headers=H)
    assert r.status_code == 200 and r.json()["created"] and r.json()["kind"] == "pdf"
    r = client.post("/api/upload", files={"file": ("evil.pdf", b"MZ\x90\x00binary", "application/pdf")}, headers=H)
    assert r.status_code == 415
    chat = "12/10/26, 9:15 pm - Riya: CN viva on 24 Oct\n".encode()
    r = client.post("/api/upload", files={"file": ("WhatsApp Chat with CSE.txt", chat, "text/plain")}, headers=H)
    assert r.json()["kind"] == "whatsapp"
    r = client.post("/api/upload", files={"file": ("notes.txt", b"just notes", "text/plain")}, headers=H)
    assert r.status_code == 422
    small = replace(env["settings"], max_upload_bytes=100)
    env["app"].dependency_overrides[routes.settings_dep] = lambda: small
    r = client.post("/api/upload", files={"file": ("big.pdf", b"%PDF-" + b"0" * 200, "application/pdf")}, headers=H)
    assert r.status_code == 413


def test_sources_delete_cascade_and_isolation(env):
    alice, bob = login(env, env["alice"]), login(env, env["bob"])
    r = alice.post("/api/upload", files={"file": ("c.pdf", make_pdf(["DBMS midsem 21 Oct"]), "application/pdf")},
                   headers=H)
    sid = r.json()["source_id"]
    worker.drain(env["conn"], FakeEmbedder())
    [src] = alice.get("/api/sources").json()
    assert src["status"] == "done" and src["segments"] == 1
    assert bob.get("/api/sources").json() == []
    assert bob.delete(f"/api/sources/{sid}", headers=H).status_code == 404  # Bob can't delete Alice's
    assert bob.get("/api/search?q=DBMS").json() == []
    path = Path(repo.get_source(env["conn"], env["alice"], sid).file_path)
    assert alice.delete(f"/api/sources/{sid}", headers=H).json() == {"deleted": True}
    assert alice.get("/api/sources").json() == [] and not path.exists()
    for t in ("segments", "chunks", "chunks_fts", "chunks_vec"):
        assert env["conn"].execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0


def test_search_ask_and_events(env):
    conn, a = env["conn"], env["alice"]
    for text, when in (("DBMS midsem is on 21 Oct", datetime(2026, 9, 25, tzinfo=IST)),
                       ("Riya says DBMS midsem is on 23 Oct", datetime(2026, 10, 14, tzinfo=IST))):  # no update wording
        repo.ingest_source(conn, a, "telegram", repo.content_hash(text), [SegmentIn(text=text, observed_at=when)])
    worker.drain(conn, FakeEmbedder())
    soon = datetime.now(timezone.utc) + timedelta(days=4)
    for i, seg in enumerate(conn.execute("SELECT id FROM segments ORDER BY id").fetchall()):
        repo.add_event(conn, a, EventIn(segment_id=seg[0], title="DBMS midsem", type="exam", subject="DBMS",
                                        due_at=soon + timedelta(days=2 * i), confidence=0.9))
    client = login(env, a)
    hits = client.get("/api/search?q=DBMS midsem&type=telegram").json()
    assert hits and hits[0]["source"]["type"] == "telegram" and "label" in hits[0]
    assert client.get("/api/search?q=DBMS&type=gmail").json() == []
    ans = client.post("/api/ask", json={"question": "When is the DBMS midsem?"}, headers=H).json()
    assert ans["answer"] == "DBMS midsem is on 23 Oct [1]." and ans["citations"][0]["n"] == 1
    events = client.get("/api/events").json()
    assert [e["status"] for e in events] == ["conflict", "conflict"] and events[0]["source"]["type"] == "telegram"


def test_gmail_connect(env):
    client = login(env, env["alice"])
    r = client.get("/gmail/connect", follow_redirects=False)
    assert r.status_code == 503 and "not configured" in r.text
    configured = replace(env["settings"], google_client_id="cid", google_client_secret="sec")
    env["app"].dependency_overrides[routes.settings_dep] = lambda: configured
    r = client.get("/gmail/connect", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    anon = TestClient(env["app"])
    assert anon.get("/gmail/connect", follow_redirects=False).status_code == 400
    token = repo.create_login_token(env["conn"], env["alice"])
    assert anon.get(f"/gmail/connect?token={token}", follow_redirects=False).status_code == 303
    assert anon.get("/gmail/callback?error=access_denied").status_code == 400
    assert "expired" in anon.get("/gmail/callback?state=bogus&code=x").text
