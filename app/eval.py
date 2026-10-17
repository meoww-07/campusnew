"""Search/answer evaluation on the seed data.

    python -m app.eval [--k 5] [--no-answers] [--no-llm] [--max-distance 0.5] [--db PATH]

By default builds a fresh temporary database, loads the seed data for a test
user, processes it, then for each question in seed/eval.jsonl:
  * search hit@k: some top-k chunk is from the expected source and contains
    the expected text (several acceptable answers may be listed)
  * prints the LLM answer for manual review (if an LLM is configured)
"""
from __future__ import annotations

import argparse
import json
import logging
import tempfile
from dataclasses import replace
from pathlib import Path

from app.config import get_settings
from app.db import init_db
from app.llm import make_llm
from app.organize.indexer import FastEmbedder
from app.retrieve.answer import ask
from app.search.hybrid import SearchResult, search
from app.seed import SEED_DIR, load_seed, process

EVAL_USER_CHAT_ID = -1


def is_hit(result: SearchResult, expected: dict) -> bool:
    src = expected["source"]
    ok_source = result.hit.source_type == src if src in ("whatsapp", "telegram", "gmail") \
        else (result.hit.source_title or "") == src
    return ok_source and expected["text"].lower() in result.hit.text.lower()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--no-answers", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="no LLM extraction or answers")
    ap.add_argument("--max-distance", type=float, default=None, help="override VECTOR_MAX_DISTANCE")
    ap.add_argument("--db", type=Path, default=None, help="existing DB with seed data already loaded")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    settings = get_settings()
    max_distance = args.max_distance if args.max_distance is not None else settings.vector_max_distance
    llm = None if args.no_llm else make_llm(settings)
    embedder = FastEmbedder()

    tmp = None
    if args.db is None:
        tmp = tempfile.TemporaryDirectory()
        settings = replace(settings, db_path=Path(tmp.name) / "eval.db", files_dir=Path(tmp.name) / "files")
    conn = init_db(settings.db_path if args.db is None else args.db)
    if args.db is None:
        user_id = load_seed(conn, settings, EVAL_USER_CHAT_ID, "Eval User")
        process(conn, embedder, llm)
    else:
        user_id = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()[0]

    questions = [json.loads(line) for line in (SEED_DIR / "eval.jsonl").read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    hits = 0
    print(f"\n=== Search recall@{args.k} (VECTOR_MAX_DISTANCE={max_distance}) ===")
    for i, q in enumerate(questions, 1):
        results = search(conn, embedder, user_id, q["q"], k=args.k, max_distance=max_distance)
        rank = next((r for r, res in enumerate(results, 1) if any(is_hit(res, e) for e in q["expected"])), None)
        hits += rank is not None
        print(f"{'OK ' if rank else 'MISS'} {i:2d}. {q['q']}" + (f"  (rank {rank})" if rank else ""))
        if rank is None and results:
            print(f"        top: {results[0].hit.text[:90]!r}")
    print(f"\nrecall@{args.k} = {hits}/{len(questions)} = {hits / len(questions):.2f}")

    if llm and not args.no_answers:
        print("\n=== Answers (manual review) ===")
        for i, q in enumerate(questions, 1):
            out = ask(conn, embedder, llm, user_id, q["q"], max_distance=max_distance)
            print(f"\n{i}. Q: {q['q']}\nA: {out.answer}")
            for c in out.citations:
                print(f"   [{c.n}] {c.label}")
    elif not args.no_answers:
        print("\n(No LLM configured: set LLM_BASE_URL/LLM_MODEL/LLM_API_KEY to print answers.)")
    conn.close()
    if tmp:
        tmp.cleanup()


if __name__ == "__main__":
    main()
