from datetime import datetime, timezone

import pytest

from app import repo
from app.ingest.upload import store_file
from app.organize import extract_text, worker
from tests.conftest import seg
from tests.fakes import FakeEmbedder, make_pdf

NOW = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)


def _count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_text_job_is_indexed(conn, users):
    a, _ = users
    emb = FakeEmbedder()
    res = repo.ingest_source(conn, a, "telegram", "h1", [seg("Compiler design lab record due Friday")])
    assert worker.run_once(conn, emb)
    assert repo.get_job(conn, a, res.job_id).status == "done"
    assert _count(conn, "chunks") == _count(conn, "chunks_fts") == _count(conn, "chunks_vec") == 1
    [hit] = repo.fts_search(conn, a, '"compiler"')
    assert repo.vec_search(conn, a, emb.embed_query("compiler lab record"), 1)[0][0] == hit[0]
    assert not worker.run_once(conn, emb)  # queue empty


def test_pdf_job_makes_page_segments(conn, users, tmp_path):
    a, _ = users
    pdf = make_pdf(["Circular: Mid-semester exams begin 20 October 2026 in the main block.",
                    "Fee payment deadline is 25 October 2026 for all students."])
    res = store_file(conn, tmp_path, a, pdf, source_type="telegram", filename="circular.pdf",
                     observed_at=NOW, locator={"chat_id": 1, "message_id": 9}, caption="Important!")
    emb = FakeEmbedder()
    worker.run_once(conn, emb)
    assert repo.get_job(conn, a, res.job_id).status == "done"
    segs = repo.list_segments(conn, a, res.source_id)
    assert [s.text for s in segs][0] == "Important!"
    assert [s.locator.get("page") for s in segs] == [None, 1, 2]
    assert segs[1].locator["message_id"] == 9 and segs[1].locator["file_name"] == "circular.pdf"
    assert all(s.observed_at == "2026-10-16T09:00:00+00:00" for s in segs)
    assert emb.calls == [3]  # all chunks embedded in ONE batch
    assert repo.fts_search(conn, a, '"fee"')


def test_retry_is_idempotent(conn, users, tmp_path):
    a, _ = users
    res = store_file(conn, tmp_path, a, make_pdf(["Page one has plenty of text for indexing purposes."]),
                     source_type="upload", filename="x.pdf", observed_at=NOW)
    job = repo.claim_job(conn)
    emb = FakeEmbedder()
    worker.process_job(conn, job, emb)
    worker.process_job(conn, job, emb)  # e.g. crashed before complete_job, reclaimed
    assert len(repo.list_segments(conn, a, res.source_id)) == 1
    assert _count(conn, "chunks") == _count(conn, "chunks_vec") == 1


def test_failures_are_recorded_and_retried(conn, users, tmp_path, monkeypatch):
    a, _ = users
    from tests.test_extract_upload import _png
    res = store_file(conn, tmp_path, a, _png("hello"), source_type="telegram", filename="p.png", observed_at=NOW)
    monkeypatch.setattr(extract_text, "ocr_available", lambda: False)
    emb = FakeEmbedder()
    for expected in ("pending", "pending", "failed"):
        worker.run_once(conn, emb)
        job = repo.get_job(conn, a, res.job_id)
        assert job.status == expected and "OcrUnavailable" in job.last_error
    assert not worker.run_once(conn, emb)


def test_image_job_uses_caption_and_ocr(conn, users, tmp_path, monkeypatch):
    a, _ = users
    from tests.test_extract_upload import _png
    res = store_file(conn, tmp_path, a, _png(), source_type="telegram", filename="p.png",
                     observed_at=NOW, caption="Notice board photo")
    monkeypatch.setattr(extract_text, "ocr_image_file", lambda path: "Lab exam shifted to Thursday")
    worker.run_once(conn, FakeEmbedder())
    [s] = repo.list_segments(conn, a, res.source_id)
    assert s.text == "Caption: Notice board photo\n\nLab exam shifted to Thursday" and s.locator["ocr"] is True


RUBRIC = """1. Problem understanding & relevance 10 Clearly defines the user, pain point and constraints
2. Functionality & completeness 20 Core user journey works end-to-end
3. Technical implementation & architecture 15 Sound design, appropriate technology choices
4. Innovation & differentiation 10 Distinctive approach or useful insight, not a generic template
5. Validation, testing & evidence 15 Live tests, realistic sample data, edge cases
6. Usability & accessibility 10 Clear, learnable flow, readable interface, mobile behaviour
7. Impact, feasibility & scalability 10 Could be adopted by real students at scale
8. Presentation & demo 10 Clear story, working live demo within time"""


def test_ocr_rubric_is_line_chunked_and_summary_is_searchable(conn, users, tmp_path, monkeypatch):
    """Reported: an OCR'd judging rubric was saved but hard to find."""
    from app.organize import chunker
    from tests.fakes import FakeLLM
    from tests.test_extract_upload import _png
    assert chunker.is_structured(RUBRIC) and len(chunker.chunk_lines(RUBRIC)) >= 2
    a, _ = users
    res = store_file(conn, tmp_path, a, _png(), source_type="telegram", filename="photo_39.jpg",
                     observed_at=NOW, caption="Photo from Mohit")
    monkeypatch.setattr(extract_text, "ocr_image_file", lambda path: RUBRIC)
    llm = FakeLLM('{"summary": "Hackathon judging rubric: 8 evaluation criteria with marks", '
                  '"category": "resource", "subject": null, "importance": "high", "events": []}')
    worker.run_once(conn, FakeEmbedder(), llm)
    assert '"SOURCE": "photo or scanned page' in llm.prompts[0][1]  # the LLM is told it's OCR text
    [s] = repo.list_segments(conn, a, res.source_id)
    chunks = repo.list_chunks(conn, a, s.id)
    assert any(c.char_start == repo.SUMMARY_CHUNK for c in chunks) and len(chunks) >= 3
    # A query that only matches the summary finds the photo, and shows its full text.
    [hit] = repo.fts_search(conn, a, '"rubric"')
    shown = repo.get_chunk_hits(conn, a, [hit[0]])[hit[0]].text
    assert shown.startswith("Hackathon judging rubric") and "Innovation & differentiation 10" in shown


def test_reindex_rebuilds_chunks(conn, users):
    a, _ = users
    res = repo.ingest_source(conn, a, "upload", "r", [seg(RUBRIC)])
    worker.drain(conn, FakeEmbedder())
    [s] = repo.list_segments(conn, a, res.source_id)
    repo.apply_extraction(conn, a, s.id, summary="Hackathon judging rubric", category="resource",
                          subject=None, importance="high")
    before = len(repo.list_chunks(conn, a, s.id))
    assert worker.reindex_user(conn, a, FakeEmbedder()) == before + 1  # same chunks + the summary chunk
    assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == before + 1


@pytest.mark.slow
def test_real_fastembed():
    from app.organize.indexer import FastEmbedder
    e = FastEmbedder()
    docs = e.embed(["The DBMS exam is on Monday", "The canteen serves dosa"])
    q = e.embed_query("when is the database exam?")
    assert len(q) == 384 and abs(sum(x * x for x in q) - 1) < 1e-3
    sim = [sum(x * y for x, y in zip(q, d)) for d in docs]
    assert sim[0] > sim[1]
