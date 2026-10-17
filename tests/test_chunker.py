from app.organize.chunker import MAX_TOKENS, OVERLAP_TOKENS, chunk_text, count_tokens


def test_short_text_is_one_chunk_with_offsets():
    text = "  The DBMS midsem is on 20 Oct. Bring your ID card!  "
    [c] = chunk_text(text)
    assert c.text == "The DBMS midsem is on 20 Oct. Bring your ID card!"
    assert text[c.char_start:c.char_end] == c.text


def test_empty_text():
    assert chunk_text("   \n ") == []


def _long_text(n_sentences: int = 120) -> str:
    return " ".join(f"Sentence number {i} talks about lab {i % 7} and assignment {i}." for i in range(n_sentences))


def test_long_text_splits_on_sentences_with_overlap():
    text = _long_text()
    chunks = chunk_text(text)
    assert len(chunks) > 2
    for c in chunks:
        assert text[c.char_start:c.char_end] == c.text
        assert count_tokens(c.text) <= MAX_TOKENS
        assert c.text.endswith(".")  # sentence boundary
    assert chunks[0].char_start == 0 and chunks[-1].char_end == len(text)
    for a, b in zip(chunks, chunks[1:]):
        assert b.char_start < a.char_end  # overlapping
        overlap = text[b.char_start:a.char_end]
        assert 0 < count_tokens(overlap) <= OVERLAP_TOKENS


def test_newlines_are_boundaries():
    text = "\n".join(f"Notice line {i} without a full stop" for i in range(200))
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(text[c.char_start:c.char_end] == c.text for c in chunks)
    assert all(c.text.startswith("Notice line") for c in chunks)


def test_giant_sentence_is_hard_split():
    text = " ".join(["word"] * 1000)
    chunks = chunk_text(text)
    assert len(chunks) >= 3
    assert all(count_tokens(c.text) <= MAX_TOKENS for c in chunks)
    assert chunks[-1].char_end == len(text)
