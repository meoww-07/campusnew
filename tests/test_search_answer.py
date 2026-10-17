from datetime import datetime, timedelta, timezone

import pytest

from app import repo
from app.models import EventIn, SegmentIn
from app.organize import worker
from app.retrieve import answer as ans
from app.search.hybrid import SearchFilters, sanitize_fts, search
from tests.conftest import IST
from tests.fakes import FakeEmbedder, FakeLLM


@pytest.mark.parametrize("q,expected", [
    ("When is the DBMS exam?", '"dbms" OR "exam"'),
    ('dbms" OR NEAR(x) * col:val -', '"dbms" OR "near" OR "x" OR "col" OR "val"'),
    ("what is it", '"what" OR "is" OR "it"'),  # all stopwords: keep them
    ("?!*", None),
])
def test_sanitize_fts(q, expected):
    assert sanitize_fts(q) == expected


@pytest.fixture
def corpus(conn, users):
    a, b = users
    emb = FakeEmbedder()
    items = [
        ("telegram", "DBMS midsem is on 21 Oct at 10am in LH-3", datetime(2026, 9, 25, 15, tzinfo=IST)),
        ("telegram", "DBMS midsem postponed to 23 Oct, 2 pm, LH-3", datetime(2026, 10, 14, 19, tzinfo=IST)),
        ("upload", "Fee payment deadline is 31 October 2026", datetime(2026, 10, 5, 10, tzinfo=IST)),
        ("whatsapp", "Mess food was terrible today", datetime(2026, 10, 8, 20, tzinfo=IST)),
    ]
    for typ, text, when in items:
        repo.ingest_source(conn, a, typ, repo.content_hash(text),
                           [SegmentIn(text=text, observed_at=when, locator={"chat_id": 1001, "message_id": 1})])
    repo.ingest_source(conn, b, "telegram", "bob", [SegmentIn(text="Bob's DBMS exam is in December",
                                                               observed_at=datetime(2026, 10, 1, tzinfo=IST))])
    worker.drain(conn, emb)
    return a, emb


def test_hybrid_ranks_and_scopes(conn, corpus):
    a, emb = corpus
    res = search(conn, emb, a, "when is the DBMS midsem", k=5, max_distance=2.0)
    texts = [r.hit.text for r in res]
    assert all("Bob" not in t for t in texts)
    assert set(texts[:2]) == {"DBMS midsem is on 21 Oct at 10am in LH-3", "DBMS midsem postponed to 23 Oct, 2 pm, LH-3"}
    assert res[0].score >= res[-1].score and res[0].fts_rank is not None


def test_vector_cutoff_and_filters(conn, corpus):
    a, emb = corpus
    # Tight cutoff: only lexical hits survive, so unrelated text never appears.
    res = search(conn, emb, a, "fee deadline", k=10, max_distance=0.0001)
    assert [r.hit.text for r in res] == ["Fee payment deadline is 31 October 2026"]
    res = search(conn, emb, a, "DBMS fee mess", max_distance=2.0, filters=SearchFilters(source_types=["upload"]))
    assert {r.hit.source_type for r in res} == {"upload"}
    res = search(conn, emb, a, "DBMS midsem", max_distance=2.0,
                 filters=SearchFilters(start=datetime(2026, 10, 1, tzinfo=timezone.utc)))
    assert res[0].hit.text == "DBMS midsem postponed to 23 Oct, 2 pm, LH-3"
    assert all(r.hit.observed_at >= "2026-10-01" for r in res)  # the 25 Sep message is filtered out


def test_category_filter(conn, corpus):
    a, emb = corpus
    seg_id = repo.fts_search(conn, a, '"fee"')[0][0]
    seg = repo.get_chunk_hits(conn, a, [seg_id])[seg_id].segment_id
    repo.apply_extraction(conn, a, seg, summary="fee", category="deadline", subject=None, importance="high")
    res = search(conn, emb, a, "fee DBMS", max_distance=2.0, filters=SearchFilters(category="deadline"))
    assert [r.hit.text for r in res] == ["Fee payment deadline is 31 October 2026"]


def test_ask_builds_numbered_context_and_citations(conn, corpus):
    a, emb = corpus
    llm = FakeLLM("It was moved from 21 Oct [1] to 23 Oct, 2 pm [2]. Also [9] is bogus.")
    out = ans.ask(conn, emb, llm, a, "When is the DBMS midsem?", max_distance=2.0)
    system, prompt, json_mode = llm.prompts[0]
    assert "cite every factual claim" in system.lower() and not json_mode
    assert "[1] Telegram" in prompt and "QUESTION:\nWhen is the DBMS midsem?" in prompt
    assert "Today is" in prompt
    assert "[9]" not in out.answer  # invalid citation stripped
    assert [c.n for c in out.citations] == [1, 2]
    assert all("Bob" not in c.snippet for c in out.context)


def test_ask_includes_events_even_if_not_retrieved(conn, corpus):
    a, emb = corpus
    text = "Compiler design quiz 2 next Tuesday 9 am"
    res = repo.ingest_source(conn, a, "telegram", repo.content_hash(text),
                             [SegmentIn(text=text, observed_at=datetime(2026, 10, 16, 10, tzinfo=IST))])
    worker.drain(conn, emb)
    seg = repo.list_segments(conn, a, res.source_id)[0]
    repo.add_event(conn, a, EventIn(segment_id=seg.id, title="CD Quiz 2", type="exam", subject="CD",
                                    due_at=datetime.now(timezone.utc) + timedelta(days=3), confidence=0.9))
    llm = FakeLLM("Quiz 2 [1].")
    # A query whose words match the event title, but with retrieval effectively off.
    ans.ask(conn, emb, llm, a, "quiz", max_distance=0.0)
    prompt = llm.prompts[0][1]
    assert "KNOWN EVENTS" in prompt and "CD Quiz 2 (exam, CD)" in prompt


def test_ask_not_found_skips_llm(conn, corpus):
    a, emb = corpus
    llm = FakeLLM("should not be called")
    out = ans.ask(conn, emb, llm, a, "zzzz qqqq", max_distance=0.0)
    assert out.answer == ans.NOT_FOUND and llm.prompts == []


def test_ask_without_llm_lists_sources(conn, corpus):
    a, emb = corpus
    out = ans.ask(conn, emb, None, a, "fee deadline", max_distance=2.0)
    assert "No LLM configured" in out.answer and out.citations


def test_telegram_reply_target():
    c = ans.Citation(n=1, chunk_id=1, source_id=1, source_type="telegram", label="x", snippet="s",
                     observed_at="2026-10-01T00:00:00+00:00", locator={"chat_id": 5, "message_id": 77})
    a = ans.Answer(answer="x [1]", citations=[c], context=[c])
    assert ans.telegram_reply_target(a, 5) == 77
    assert ans.telegram_reply_target(a, 6) is None


def test_uncited_answer_is_retried(conn, corpus):
    a, emb = corpus
    llm = FakeLLM(["The fee is due on 31 October.", "The fee is due on 31 October [1]."])
    out = ans.ask(conn, emb, llm, a, "fee deadline", max_distance=2.0)
    assert len(llm.prompts) == 2 and "no [n] citations" in llm.prompts[1][1]
    assert out.answer.endswith("[1].") and [c.n for c in out.citations] == [1]


def test_not_found_answer_is_not_retried(conn, corpus):
    a, emb = corpus
    llm = FakeLLM([ans.NOT_FOUND])
    assert ans.ask(conn, emb, llm, a, "fee deadline", max_distance=2.0).citations == []
    assert len(llm.prompts) == 1
