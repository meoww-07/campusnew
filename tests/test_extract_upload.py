import io
from datetime import datetime, timezone

import pytest
from PIL import Image, ImageDraw

from app import repo
from app.ingest.upload import UploadRejected, detect_kind, store_file
from app.organize import extract_text
from tests.fakes import make_pdf

NOW = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)
needs_ocr = pytest.mark.skipif(not extract_text.ocr_available(), reason="Tesseract not installed")


def _png(text: str = "", size=(400, 120)) -> bytes:
    img = Image.new("RGB", size, "white")
    if text:
        ImageDraw.Draw(img).text((10, 40), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- magic bytes ----------

def test_detect_kind():
    assert detect_kind(make_pdf(["hi"])) == "pdf"
    assert detect_kind(_png()) == "png"
    assert detect_kind(b"\xff\xd8\xff\xe0rest") == "jpg"
    assert detect_kind("12/10/26, 9:15 pm - Riya: hello ✓".encode()) == "txt"
    assert detect_kind(b"MZ\x90\x00\x03") is None  # Windows exe
    assert detect_kind(b"PK\x03\x04zip") is None  # zip/docx (has NUL later)
    assert detect_kind(b"\x00\x01\x02") is None


# ---------- store_file ----------

def test_store_file_saves_and_queues(conn, users, tmp_path):
    a, _ = users
    data = make_pdf(["Exam circular"])
    res = store_file(conn, tmp_path, a, data, source_type="upload", filename="circ.pdf",
                     observed_at=NOW, locator={"x": 1}, caption="see page 2")
    src = repo.get_source(conn, a, res.source_id)
    assert src.title == "circ.pdf" and src.file_path.endswith(".pdf")
    assert open(src.file_path, "rb").read() == data
    job = repo.get_job(conn, a, res.job_id)
    assert job.kind == "pdf"
    assert job.payload == {"observed_at": "2026-10-16T09:00:00+00:00",
                           "locator": {"x": 1, "file_name": "circ.pdf"}, "caption": "see page 2"}
    dup = store_file(conn, tmp_path, a, data, source_type="upload", filename="again.pdf", observed_at=NOW)
    assert not dup.created and dup.source_id == res.source_id


@pytest.mark.parametrize("data,msg", [
    (b"MZ\x90\x00", "Unsupported"),
    (b"", "empty"),
    (b"plain text file", "Unsupported"),  # txt not allowed by default
])
def test_store_file_rejects(conn, users, tmp_path, data, msg):
    a, _ = users
    with pytest.raises(UploadRejected, match=msg):
        store_file(conn, tmp_path, a, data, source_type="upload", filename="f", observed_at=NOW)
    assert repo.list_sources(conn, a) == []


def test_store_file_size_limit(conn, users, tmp_path):
    a, _ = users
    with pytest.raises(UploadRejected, match="too large"):
        store_file(conn, tmp_path, a, b"%PDF-" + b"0" * 2000, source_type="upload", filename="big.pdf",
                   observed_at=NOW, max_bytes=1000)
    assert not any(tmp_path.rglob("*.pdf"))


def test_store_file_removes_file_if_db_fails(conn, users, tmp_path, monkeypatch):
    a, _ = users
    monkeypatch.setattr(repo, "ingest_source", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        store_file(conn, tmp_path, a, make_pdf(["x"]), source_type="upload", filename="x.pdf", observed_at=NOW)
    assert not any(tmp_path.rglob("*.pdf"))


# ---------- extraction ----------

def test_extract_pdf_pages(tmp_path):
    p = tmp_path / "c.pdf"
    p.write_bytes(make_pdf(["Mid-semester examinations will begin on 20 October 2026.", "Page two text here."]))
    pages = extract_text.extract_pdf(p)
    assert [pg.page for pg in pages] == [1, 2]
    assert "20 October 2026" in pages[0].text and not pages[0].ocr


def test_blank_page_without_ocr_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_text, "ocr_available", lambda: False)
    p = tmp_path / "c.pdf"
    p.write_bytes(make_pdf(["Some real text on the first page of the notice.", None]))
    pages = extract_text.extract_pdf(p)
    assert pages[1].text == "" and not pages[1].ocr


def test_ocr_unavailable_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_text, "ocr_available", lambda: False)
    p = tmp_path / "x.png"
    p.write_bytes(_png("hello"))
    with pytest.raises(extract_text.OcrUnavailable):
        extract_text.ocr_image_file(p)


def test_preprocess_upscales_only_small_images():
    small = extract_text.preprocess(Image.open(io.BytesIO(_png("abc", size=(100, 50)))))
    assert small.size == (200, 100) and small.mode == "L"
    big = extract_text.preprocess(Image.new("RGB", (2000, 800), "white"))
    assert big.size == (2000, 800)


def _notice(width=1100, height=420, rotate=2.5) -> Image.Image:
    """A phone-style photo of a notice: uneven lighting, slight tilt, blur."""
    import numpy as np
    from PIL import ImageFilter, ImageFont
    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except OSError:
        font = ImageFont.load_default(size=34)
    xs = np.linspace(0, 1, width)[None, :]
    ys = np.linspace(0, 1, height)[:, None]
    img = Image.fromarray((225 - 70 * xs - 20 * ys).astype("uint8"))
    d = ImageDraw.Draw(img)
    for i, t in enumerate(["NOTICE", "Compiler Design Lab exam will be held on",
                           "29 October 2026 at 10:30 am in Lab 4.", "Bring your lab record and college ID card."]):
        d.text((40, 30 + i * 70), t, fill=40, font=font)
    return img.rotate(rotate, expand=True, fillcolor=150).filter(ImageFilter.GaussianBlur(0.8)).convert("RGB")


@needs_ocr
@pytest.mark.parametrize("scale", [1.0, 0.5])
def test_ocr_photo_with_uneven_lighting_keeps_dates(tmp_path, scale):
    img = _notice()
    img = img.resize((int(img.width * scale), int(img.height * scale)))
    p = tmp_path / "notice.jpg"
    img.save(p, quality=65)
    text = extract_text.ocr_image_file(p)
    assert "29 October 2026" in text and "10:30" in text and "lab record" in text


@needs_ocr
def test_ocr_grey_photo_on_scanned_page(tmp_path):
    import fitz
    buf = io.BytesIO()
    _notice().save(buf, "PNG")
    doc = fitz.open()
    doc.new_page().insert_image(fitz.Rect(30, 40, 565, 260), stream=buf.getvalue())
    p = tmp_path / "scan.pdf"
    p.write_bytes(doc.tobytes())
    [page] = extract_text.extract_pdf(p)
    assert page.ocr and "29 October 2026" in page.text


def test_clean():
    assert extract_text.clean("  a   b \r\n\n\n\n c  ") == "a b\n\nc"


@needs_ocr
def test_ocr_scanned_pdf_page(tmp_path):
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    big = Image.new("RGB", (1200, 200), "white")
    ImageDraw.Draw(big).text((20, 60), "QUIZ ON MONDAY", fill="black", font_size=80)
    buf = io.BytesIO(); big.save(buf, "PNG")
    page.insert_image(fitz.Rect(50, 50, 550, 133), stream=buf.getvalue())
    p = tmp_path / "scan.pdf"; p.write_bytes(doc.tobytes())
    [pg] = extract_text.extract_pdf(p)
    assert pg.ocr and "QUIZ" in pg.text.upper()


def test_vote_majority_per_token():
    texts = ["NOTICE\n34 October at 4:05", "NOTICE\n31 October at 4:05", "NOTICE\n31 October at 4:05",
             "NOTICE\n314 October at 4:05 extra"]
    assert extract_text.vote(texts) == "NOTICE\n31 October at 4:05"
    assert extract_text.vote(["only one reading"]) == "only one reading"
