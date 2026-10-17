import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import repo
from app.config import Settings
from app.db import MIGRATIONS, connect, init_db, migrate
from app.models import EventIn, SegmentContext, SegmentIn, SpaceRules
from app.organize import spaces, subjects, worker
from app.search.hybrid import SearchFilters, search
from app.web import routes
from tests.conftest import IST
from tests.fakes import FakeEmbedder

NOW = datetime.now(timezone.utc)
A = subjects.load_aliases()


def seg(conn, user_id, text, *, subject=None, category="general", importance="medium", source="telegram",
        locator=None, when=None, events=()):
    """Ingest + index + 'LLM extraction' for one segment; returns its id."""
    res = repo.ingest_source(conn, user_id, source, repo.content_hash(text + str(user_id)),
                             [SegmentIn(text=text, observed_at=when or NOW - timedelta(days=1), locator=locator or {})],
                             title=text[:40])
    seg_id = repo.list_segments(conn, user_id, res.source_id)[0].id
    from app.organize.indexer import index_segments
    index_segments(conn, user_id, repo.list_segments(conn, user_id, res.source_id), FakeEmbedder())
    repo.apply_extraction(conn, user_id, seg_id, summary=None, category=category, subject=subject,
                          importance=importance, events=[EventIn(segment_id=seg_id, **e) for e in events])
    return seg_id


def names(conn, user_id):
    return {s.name for s in repo.list_spaces(conn, user_id)}


def items(conn, user_id, name):
    space = next(s for s in repo.list_spaces(conn, user_id) if s.name == name)
    return repo.space_segment_ids(conn, user_id, space.id)


# ---------- subject normalization ----------

@pytest.mark.parametrize("raw,expected", [
    ("dbms", "DBMS"), ("Database Management Systems", "DBMS"), ("DBMS lab", "DBMS"), ("CS301", "DBMS"),
    ("DBMS (CS301)", "DBMS"), ("operating systems", "OS"), ("Computer Networks Lab", "CN"),
    ("compiler design quiz", "Compiler Design"), ("Data Structures", "DSA"),
    ("Cloud Computing", "Cloud Computing"), ("iot", "IOT"),
    ("DBMS, OS", None), ("", None), (None, None), ("Placement", None), ("CS399", None),
])
def test_normalize_subject(raw, expected):
    assert subjects.normalize(raw, A) == expected


def test_normalize_uses_known_names_and_custom_alias_file(tmp_path):
    assert subjects.normalize("Wireless Sensor Networks", {}, known=["WSN"]) == "WSN"
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"Web Tech": ["web technology", "wt", "cs402"]}), encoding="utf-8")
    custom = subjects.load_aliases(str(path))
    assert subjects.normalize("WT", custom) == "Web Tech" and subjects.normalize("CS402", custom) == "Web Tech"


# ---------- auto spaces ----------

def test_subject_space_needs_two_items(conn, users):
    a, _ = users
    s1 = seg(conn, a, "DBMS midsem syllabus chapters 1-7", subject="DBMS")
    spaces.assign_segments(conn, a, [s1])
    assert "DBMS" not in names(conn, a)  # no one-item spaces
    s2 = seg(conn, a, "Database Management Systems assignment 2", subject="Database Management Systems")
    spaces.assign_segments(conn, a, [s2])
    assert items(conn, a, "DBMS") == {s1, s2}  # earlier item included on creation
    s3 = seg(conn, a, "DBMS lab record", subject="DBMS lab")
    spaces.assign_segments(conn, a, [s3])
    assert items(conn, a, "DBMS") == {s1, s2, s3}


def test_category_spaces_and_multiple_memberships(conn, users):
    a, _ = users
    exam = seg(conn, a, "OS midsem on 22 Oct", subject="OS", category="exam",
               events=[{"title": "OS midsem", "type": "exam", "subject": "OS",
                        "due_at": NOW + timedelta(days=5), "confidence": 0.9}])
    mixed = seg(conn, a, "OS assignment 3 due 19 Oct; DBMS quiz on Monday", subject="OS", category="general",
                events=[{"title": "OS Assignment 3", "type": "deadline", "subject": "OS",
                         "due_at": NOW + timedelta(days=2), "confidence": 0.9},
                        {"title": "DBMS quiz", "type": "exam", "subject": "DBMS",
                         "due_at": NOW + timedelta(days=3), "confidence": 0.9}])
    spaces.assign_segments(conn, a, [exam, mixed])
    assert {"Exams", "Deadlines", "Notices", "Resources", "Placements", "OS"} <= names(conn, a)
    assert items(conn, a, "Exams") == {exam, mixed}  # mixed has an exam event
    assert items(conn, a, "Deadlines") == {mixed}
    assert items(conn, a, "OS") == {exam, mixed}
    overview = {s["name"]: s for s in repo.list_spaces_overview(conn, a)}
    assert overview["OS"]["item_count"] == 2 and overview["OS"]["next_event"]["title"] == "OS Assignment 3"


def test_assignment_is_idempotent(conn, users):
    a, _ = users
    ids = [seg(conn, a, f"CN lab experiment {i}", subject="CN") for i in range(3)]
    spaces.assign_segments(conn, a, ids)
    assert spaces.assign_segments(conn, a, ids) == {}
    assert len(items(conn, a, "CN")) == 3


# ---------- rules ----------

def ctx(**kw) -> SegmentContext:
    base = dict(segment_id=1, text="TCS placement drive registration closes Friday", observed_at=NOW.isoformat(),
                source_type="gmail", locator={"from": "Placement Cell <placement@college.edu.in>"})
    return SegmentContext(**{**base, **kw})


@pytest.mark.parametrize("rules,expected", [
    ({"any_keywords": ["placement", "internship"]}, True),
    ({"any_keywords": ["internship"]}, False),
    ({"all_keywords": ["tcs", "drive"]}, True),
    ({"all_keywords": ["tcs", "infosys"]}, False),
    ({"any_keywords": ["place"]}, False),  # whole words only
    ({"senders": ["placement@college.edu.in"]}, True),
    ({"senders": ["@college.edu.in"]}, True),
    ({"senders": ["college.edu.in"]}, True),
    ({"senders": ["@other.edu"]}, False),
    ({"senders": ["placement cell"]}, True),
    ({"source_types": ["whatsapp"]}, False),
    ({"any_keywords": ["tcs"], "source_types": ["gmail"]}, True),
    ({"categories": ["placement"]}, False),
    ({"date_from": (NOW + timedelta(days=1)).isoformat()}, False),
    ({}, False),  # empty rules match nothing
])
def test_match_rules(rules, expected):
    assert spaces.match_rules(SpaceRules(**rules), ctx())[0] is expected


def test_similar_to_threshold():
    rules = SpaceRules(similar_to="campus recruitment", similar_max_distance=0.4)
    assert spaces.match_rules(rules, ctx(), distance=0.2) == (True, 0.8)
    assert spaces.match_rules(rules, ctx(), distance=0.5)[0] is False
    assert spaces.match_rules(rules, ctx(), distance=None)[0] is False


def test_rules_reject_unknown_fields():
    with pytest.raises(Exception):
        SpaceRules.model_validate({"keywords": ["x"]})


def test_custom_space_backfill_job_and_rule_changes(conn, users):
    a, _ = users
    hit = seg(conn, a, "Infosys internship applications open till Friday", source="gmail",
              locator={"from": "placement@college.edu.in"})
    miss = seg(conn, a, "Canteen menu for this week")
    space = repo.create_space(conn, a, "Internships", "custom", rules=SpaceRules(any_keywords=["internship"]))
    repo.enqueue_job(conn, a, "space_backfill", space_id=space.id)
    worker.drain(conn, FakeEmbedder())
    assert repo.space_segment_ids(conn, a, space.id) == {hit}
    repo.add_space_items(conn, a, space.id, [(miss, "user", 1.0)])  # manual item survives rule changes
    repo.update_space(conn, a, space.id, rules=SpaceRules(any_keywords=["hackathon"]))
    spaces.backfill_space(conn, a, space.id)
    assert repo.space_segment_ids(conn, a, space.id) == {miss}


def test_new_segments_hit_rules_in_worker(conn, users):
    a, _ = users
    space = repo.create_space(conn, a, "From placement cell", "custom",
                              rules=SpaceRules(senders=["@college.edu.in"]))
    res = repo.ingest_source(conn, a, "gmail", "g1", [SegmentIn(
        text="Amazon SDE drive on 30 Oct", observed_at=NOW, locator={"from": "tpo@college.edu.in"})])
    worker.drain(conn, FakeEmbedder())
    [s] = repo.list_segments(conn, a, res.source_id)
    assert repo.space_segment_ids(conn, a, space.id) == {s.id}
    assert s.id not in items(conn, a, "Placements")  # bare "drive" is too generic for the built-in keywords


def test_similar_to_rule_with_embeddings(conn, users):
    a, _ = users
    near = seg(conn, a, "campus recruitment drive for final year students")
    far = seg(conn, a, "library books return deadline")
    space = repo.create_space(conn, a, "Recruiting", "custom",
                              rules=SpaceRules(similar_to="campus recruitment drive", similar_max_distance=0.5))
    spaces.backfill_space(conn, a, space.id, FakeEmbedder())
    assert repo.space_segment_ids(conn, a, space.id) == {near}
    assert far not in repo.space_segment_ids(conn, a, space.id)


# ---------- exclusions + user actions ----------

def test_exclusion_blocks_auto_and_rules_until_user_adds(conn, users):
    a, _ = users
    s1 = seg(conn, a, "OS quiz 1", subject="OS")
    s2 = seg(conn, a, "OS quiz 2 placement prep", subject="OS")
    spaces.assign_segments(conn, a, [s1, s2])
    os_space = next(s for s in repo.list_spaces(conn, a) if s.name == "OS")
    placements = next(s for s in repo.list_spaces(conn, a) if s.name == "Placements")
    assert s2 in repo.space_segment_ids(conn, a, placements.id)  # keyword "placement"
    assert repo.remove_space_item(conn, a, os_space.id, s2) and repo.remove_space_item(conn, a, placements.id, s2)
    spaces.assign_segments(conn, a, [s1, s2])
    spaces.backfill_space(conn, a, placements.id)
    assert repo.space_segment_ids(conn, a, os_space.id) == {s1}
    assert s2 not in repo.space_segment_ids(conn, a, placements.id)
    repo.add_space_items(conn, a, os_space.id, [(s2, "user", 1.0)])  # explicit user add lifts it
    assert repo.space_segment_ids(conn, a, os_space.id) == {s1, s2}


def test_delete_rename_merge(conn, users):
    a, _ = users
    for i in range(2):
        seg(conn, a, f"CN note {i}", subject="CN")
        seg(conn, a, f"Networks lab {i}", subject="Networking Lab")
    spaces.backfill_user(conn, a)
    cn = next(s for s in repo.list_spaces(conn, a) if s.name == "CN")
    # Rename keeps auto assignment (keyed by auto_key, not name)
    repo.update_space(conn, a, cn.id, name="Computer Networks", pinned=True)
    new = seg(conn, a, "CN viva slots", subject="CN")
    spaces.assign_segments(conn, a, [new])
    assert new in repo.space_segment_ids(conn, a, cn.id)
    assert repo.list_spaces(conn, a)[0].name == "Computer Networks"  # pinned first
    # Delete: items kept as segments, space not re-created
    exams = next(s for s in repo.list_spaces(conn, a) if s.name == "Exams")
    assert repo.delete_space(conn, a, exams.id)
    spaces.ensure_category_spaces(conn, a)
    assert "Exams" not in names(conn, a)
    # Merge a custom space into the subject space
    custom = repo.create_space(conn, a, "Net misc", "custom")
    extra = seg(conn, a, "subnetting cheat sheet")
    repo.add_space_items(conn, a, custom.id, [(extra, "user", 1.0)])
    merged = repo.merge_spaces(conn, a, custom.id, cn.id)
    assert extra in repo.space_segment_ids(conn, a, merged.id)
    assert repo.get_space(conn, a, custom.id) is None
    with pytest.raises(ValueError):
        repo.merge_spaces(conn, a, cn.id, cn.id)


def test_merged_auto_space_receives_future_items(conn, users):
    a, _ = users
    for i in range(2):
        seg(conn, a, f"DSA problem set {i}", subject="DSA")
        seg(conn, a, f"Cloud computing note {i}", subject="Cloud Computing")
    spaces.backfill_user(conn, a)
    dsa = next(s for s in repo.list_spaces(conn, a) if s.name == "DSA")
    cloud = next(s for s in repo.list_spaces(conn, a) if s.name == "Cloud Computing")
    repo.merge_spaces(conn, a, cloud.id, dsa.id)
    later = seg(conn, a, "Cloud computing assignment", subject="cloud computing")
    spaces.assign_segments(conn, a, [later])
    assert later in repo.space_segment_ids(conn, a, dsa.id)
    assert "Cloud Computing" not in names(conn, a)


def test_items_pagination_and_filters(conn, users):
    a, _ = users
    ids = [seg(conn, a, f"OS item {i}", subject="OS", category="exam" if i % 2 else "notice",
               when=NOW - timedelta(days=10 - i)) for i in range(5)]
    spaces.assign_segments(conn, a, ids)
    space = next(s for s in repo.list_spaces(conn, a) if s.name == "OS")
    page1, cur = repo.list_space_items(conn, a, space.id, limit=2)
    page2, cur2 = repo.list_space_items(conn, a, space.id, limit=2, cursor=cur)
    page3, cur3 = repo.list_space_items(conn, a, space.id, limit=2, cursor=cur2)
    got = [i["segment_id"] for i in page1 + page2 + page3]
    assert got == ids[::-1] and cur3 is None  # newest first, no gaps/dupes
    exams, _ = repo.list_space_items(conn, a, space.id, category="exam")
    assert {i["segment_id"] for i in exams} == {ids[1], ids[3]}
    with pytest.raises(ValueError):
        repo.list_space_items(conn, a, space.id, cursor="garbage!!")


def test_search_scoped_to_space(conn, users):
    a, _ = users
    s1 = seg(conn, a, "OS midsem syllabus deadlocks", subject="OS")
    s2 = seg(conn, a, "OS quiz scheduling", subject="OS")
    s3 = seg(conn, a, "DBMS midsem syllabus normalization", subject="DBMS")
    spaces.assign_segments(conn, a, [s1, s2, s3])
    os_space = next(s for s in repo.list_spaces(conn, a) if s.name == "OS")
    res = search(conn, FakeEmbedder(), a, "midsem syllabus", max_distance=2.0,
                 filters=SearchFilters(space_id=os_space.id))
    assert {r.hit.segment_id for r in res} <= {s1, s2} and s1 in {r.hit.segment_id for r in res}


# ---------- one-time backfill via migration ----------

def test_migration_v4_backfills_existing_data(tmp_path):
    path = tmp_path / "old.db"
    old = connect(path)
    old.execute("PRAGMA foreign_keys = OFF")
    for i, sql in enumerate(MIGRATIONS[:3]):  # a v3 database from before Smart Spaces
        old.executescript(f"BEGIN;{sql}\nPRAGMA user_version = {i + 1};COMMIT;")
    old.execute("PRAGMA foreign_keys = ON")
    u = repo.upsert_user(old, 42, "Old User").id
    res = old.execute(
        """INSERT INTO sources (user_id, type, content_hash, created_at) VALUES (?, 'telegram', 'h', '2026-10-01T00:00:00+00:00')
           RETURNING id""", (u,)).fetchone()[0]
    for i in range(2):
        old.execute("""INSERT INTO segments (source_id, user_id, text, observed_at, subject, category, extracted_at)
                       VALUES (?, ?, ?, '2026-10-01T00:00:00+00:00', 'DBMS', 'exam', '2026-10-01T00:00:00+00:00')""",
                    (res, u, f"DBMS exam info {i}"))
    old.execute("""INSERT INTO jobs (source_id, user_id, kind, status, created_at)
                   VALUES (?, ?, 'process', 'done', '2026-10-01T00:00:00+00:00')""", (res, u))
    old.close()

    conn = init_db(path)  # runs v4: rebuild jobs (rows kept) + queue a backfill per user
    jobs = conn.execute("SELECT kind, status, source_id FROM jobs ORDER BY id").fetchall()
    assert [tuple(j) for j in jobs] == [("process", "done", res), ("spaces_backfill", "pending", None),
                                             ("resolve_conflicts", "pending", None)]
    worker.drain(conn, FakeEmbedder())
    assert {"DBMS", "Exams"} <= names(conn, u)
    assert len(items(conn, u, "DBMS")) == 2 and len(items(conn, u, "Exams")) == 2
    assert migrate(conn) == len(MIGRATIONS)


# ---------- API + isolation ----------

@pytest.fixture
def api(tmp_path):
    from app import main
    settings = Settings(db_path=tmp_path / "web.db", files_dir=tmp_path / "files", session_secret="t",
                        app_base_url="http://testserver")
    conn = init_db(settings.db_path)
    main.app.dependency_overrides[routes.settings_dep] = lambda: settings
    main.app.state.embedder, main.app.state.llm, main.app.state.telegram = FakeEmbedder(), None, None
    a, b = repo.upsert_user(conn, 1, "A").id, repo.upsert_user(conn, 2, "B").id

    def client(user_id):
        c = TestClient(main.app, headers={"X-CampusMind": "1"})
        c.get(f"/login?token={repo.create_login_token(conn, user_id)}", follow_redirects=False)
        return c
    yield conn, a, b, client(a), client(b)
    main.app.dependency_overrides.clear()
    conn.close()


def test_spaces_api_flow(api):
    conn, a, _, ca, _ = api
    s1 = seg(conn, a, "TCS placement drive on 30 Oct", source="gmail", locator={"from": "tpo@college.edu.in"})
    s2 = seg(conn, a, "OS quiz Monday", subject="OS")
    listed = ca.get("/api/spaces").json()
    assert {"Exams", "Deadlines", "Notices", "Resources", "Placements"} <= {s["name"] for s in listed}
    prev = ca.post("/api/spaces/preview", json={"senders": ["@college.edu.in"]}).json()
    assert prev["count"] == 1 and prev["examples"][0]["segment_id"] == s1
    r = ca.post("/api/spaces", json={"name": "TPO", "icon": "🎯", "rules": {"senders": ["@college.edu.in"]}})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert ca.post("/api/spaces", json={"name": "TPO"}).status_code == 409
    assert ca.post("/api/spaces", json={"name": "Bad", "rules": {"nope": 1}}).status_code == 422
    worker.drain(conn, FakeEmbedder())  # backfill job
    page = ca.get(f"/api/spaces/{sid}/items").json()
    assert [i["segment_id"] for i in page["items"]] == [s1] and page["items"][0]["spaces"]
    assert ca.post(f"/api/spaces/{sid}/items", json={"segment_id": s2}).status_code == 201
    assert ca.get(f"/api/spaces/{sid}/items?source=gmail").json()["items"][0]["segment_id"] == s1
    assert ca.delete(f"/api/spaces/{sid}/items/{s2}").json() == {"removed": True}
    assert ca.patch(f"/api/spaces/{sid}", json={"name": "Training & Placement", "pinned": True}).json()["pinned"]
    assert ca.get("/api/spaces").json()[0]["name"] == "Training & Placement"
    assert ca.get(f"/api/search?q=placement&space_id={sid}").status_code == 200
    other = ca.post("/api/spaces", json={"name": "Misc"}).json()["id"]
    assert ca.post(f"/api/spaces/{other}/merge", json={"into_id": sid}).json()["id"] == sid
    assert ca.delete(f"/api/spaces/{sid}").json() == {"deleted": True}
    assert ca.get(f"/api/spaces/{sid}/items").status_code == 404
    assert repo.get_segment(conn, a, s1) is not None  # deleting a space keeps the item


def test_spaces_api_isolation(api):
    conn, a, b, ca, cb = api
    s_a = seg(conn, a, "Alice private note about OS", subject="OS")
    s_b = seg(conn, b, "Bob note")
    space_a = ca.post("/api/spaces", json={"name": "Mine"}).json()["id"]
    ca.post(f"/api/spaces/{space_a}/items", json={"segment_id": s_a})
    space_b = cb.post("/api/spaces", json={"name": "Bobs"}).json()["id"]
    assert all(s["id"] != space_a for s in cb.get("/api/spaces").json())
    for method, url, body in [
        ("get", f"/api/spaces/{space_a}/items", None),
        ("patch", f"/api/spaces/{space_a}", {"name": "hacked"}),
        ("delete", f"/api/spaces/{space_a}", None),
        ("post", f"/api/spaces/{space_a}/items", {"segment_id": s_b}),
        ("delete", f"/api/spaces/{space_a}/items/{s_a}", None),
        ("post", f"/api/spaces/{space_a}/merge", {"into_id": space_b}),
        ("post", f"/api/spaces/{space_b}/merge", {"into_id": space_a}),
        ("get", f"/api/search?q=OS&space_id={space_a}", None),
    ]:
        r = getattr(cb, method)(url, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (method, url, r.status_code)
    # Bob can't put Alice's segment into his own space
    assert cb.post(f"/api/spaces/{space_b}/items", json={"segment_id": s_a}).status_code == 404
    assert repo.space_segment_ids(conn, a, space_a) == {s_a}
    assert repo.get_space(conn, a, space_a).name == "Mine"
    # Bob's preview never counts Alice's data
    assert cb.post("/api/spaces/preview", json={"any_keywords": ["alice"]}).json()["count"] == 0


def test_csrf_on_space_mutations(api):
    conn, a, *_ = api
    from app import main
    c = TestClient(main.app)
    c.get(f"/login?token={repo.create_login_token(conn, a)}", follow_redirects=False)
    assert c.post("/api/spaces", json={"name": "x"}).status_code == 403


def test_first_visit_backfills_users_without_spaces(api):
    conn, a, _, ca, _ = api
    for i in range(2):  # processed before Smart Spaces existed: no space rows yet
        seg(conn, a, f"Compiler design notes {i}", subject="Compiler Design")
    ca.get("/api/spaces")
    assert conn.execute("SELECT count(*) FROM jobs WHERE kind = 'spaces_backfill'").fetchone()[0] == 1
    worker.drain(conn, FakeEmbedder())
    assert "Compiler Design" in {s["name"] for s in ca.get("/api/spaces").json()}
    ca.get("/api/spaces")
    assert conn.execute("SELECT count(*) FROM jobs WHERE kind = 'spaces_backfill'").fetchone()[0] == 1


def test_subject_space_next_event_is_its_own_subject(conn, users):
    a, _ = users
    circ = seg(conn, a, "Midsem schedule: CD 27 Oct, SE 28 Oct", subject="SE",
               events=[{"title": "CS304 Compiler Design midsem", "type": "exam", "subject": "CS304",
                        "due_at": NOW + timedelta(days=3), "confidence": 0.9},
                       {"title": "CS305 Software Engineering midsem", "type": "exam", "subject": "CS305",
                        "due_at": NOW + timedelta(days=4), "confidence": 0.9}])
    other = seg(conn, a, "SE SRS due", subject="SE")
    spaces.assign_segments(conn, a, [circ, other])
    by_name = {s["name"]: s for s in spaces.list_overview(conn, a)}
    assert by_name["SE"]["next_event"]["title"] == "CS305 Software Engineering midsem"
    assert by_name["Exams"]["next_event"]["title"] == "CS304 Compiler Design midsem"
