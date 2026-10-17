"""Text extraction: PDF pages (PyMuPDF), OCR for images and scanned pages."""
from __future__ import annotations

import io
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
import numpy as np
import pytesseract
from PIL import Image, ImageFilter, ImageOps

MIN_PAGE_CHARS = 30  # below this, a PDF page is treated as scanned and OCR'd
OCR_DPI = 300


class OcrUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PageText:
    page: int  # 1-based
    text: str
    ocr: bool


_DEFAULT_LOCATIONS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    str(Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
]


def configure_tesseract(cmd: str | None = None) -> None:
    """Use TESSERACT_CMD if set, else PATH, else the usual install locations."""
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
        return
    if shutil.which(pytesseract.pytesseract.tesseract_cmd):
        return
    for candidate in _DEFAULT_LOCATIONS:
        if Path(candidate).is_file():
            pytesseract.pytesseract.tesseract_cmd = candidate
            return


configure_tesseract()  # picks up a standard install with no configuration


def ocr_available() -> bool:
    cmd = pytesseract.pytesseract.tesseract_cmd
    return Path(cmd).is_file() or shutil.which(cmd) is not None


UPSCALE_BELOW = 1500  # px on the long side; larger images are not upscaled


def preprocess(img: Image.Image, *, upscale: bool = True) -> Image.Image:
    """Grayscale -> 2x upscale (small images only) -> flatten uneven lighting -> stretch contrast.

    Measured on phone-style notice photos: a hard global threshold (and
    upscaling already-large photos) turned "29 October" into "99 October", and
    a grey photo on a white scanned page lost all its text. Dividing by a
    blurred background and letting Tesseract binarize read every case right.
    """
    gray = ImageOps.grayscale(img)
    if upscale and max(gray.size) < UPSCALE_BELOW:
        gray = gray.resize((gray.width * 2, gray.height * 2), Image.Resampling.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float32)
    background = np.asarray(gray.filter(ImageFilter.GaussianBlur(radius=max(15, gray.width // 40))),
                            dtype=np.float32)
    flat = np.clip(pixels / np.maximum(background, 1.0) * 255.0, 0, 255).astype(np.uint8)
    return ImageOps.autocontrast(Image.fromarray(flat), cutoff=1)


def _scaled(gray: Image.Image, long_side: int) -> Image.Image:
    s = long_side / max(gray.size)
    return gray.resize((int(gray.width * s), int(gray.height * s)), Image.Resampling.LANCZOS) if s > 1 else gray


def _variants(img: Image.Image, upscale: bool) -> list[Image.Image]:
    """Differently preprocessed copies; their errors are mostly independent."""
    gray = ImageOps.grayscale(img)
    if not upscale:  # 300 dpi page renders: already large
        return [preprocess(gray, upscale=False), gray, preprocess(gray.filter(ImageFilter.SHARPEN), upscale=False)]
    return [preprocess(_scaled(gray, 1600), upscale=False), preprocess(_scaled(gray, 2400), upscale=False),
            _scaled(gray, 2000), preprocess(gray.filter(ImageFilter.SHARPEN))]


def vote(texts: list[str]) -> str:
    """Line-by-line, token-by-token majority vote over several OCR readings.

    Lines/tokens are only compared when the readings agree on their count;
    otherwise the first (primary) reading is kept for that part.
    """
    readings = [[ln.split() for ln in t.splitlines() if ln.strip()] for t in texts]
    if not readings:
        return ""
    n_lines = Counter(len(r) for r in readings).most_common(1)[0][0]
    aligned = [r for r in readings if len(r) == n_lines]
    if len(aligned) < 2:
        return texts[0]
    out = []
    for i in range(n_lines):
        lines = [r[i] for r in aligned]
        n_tok = Counter(len(ln) for ln in lines).most_common(1)[0][0]
        same = [ln for ln in lines if len(ln) == n_tok]
        if len(same) < 2:
            out.append(" ".join(lines[0]))
        else:
            out.append(" ".join(Counter(col).most_common(1)[0][0] for col in zip(*same)))
    return "\n".join(out)


def ocr_image(img: Image.Image, *, upscale: bool = True) -> str:
    """OCR with a majority vote over preprocessing variants.

    On generated phone-style notices this raised exact date+time accuracy from
    16/20 (single pass) to 18/20; digits like 1->4 / 1->7 are the usual errors.
    """
    if not ocr_available():
        raise OcrUnavailable("Tesseract is not installed (set TESSERACT_CMD)")
    variants = _variants(img, upscale)
    read = lambda v: pytesseract.image_to_string(v, config="--psm 6")  # noqa: E731
    # Fast path: two cheap, independent readings that agree are trusted as-is
    # (most clear photos); only disagreement pays for the full vote.
    first, second = read(variants[0]), read(variants[-2] if len(variants) > 2 else variants[-1])
    if _norm(first) == _norm(second):
        return clean(first)
    rest = [read(v) for i, v in enumerate(variants) if i not in (0, len(variants) - 2)]
    return clean(vote([first, second, *rest]))


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def ocr_image_file(path: Path | str) -> str:
    with Image.open(path) as img:
        img.load()
        return ocr_image(img)


def extract_pdf(path: Path | str) -> list[PageText]:
    """One entry per page; pages with too little text are rendered and OCR'd."""
    pages: list[PageText] = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = clean(page.get_text("text"))
            if len(text) >= MIN_PAGE_CHARS:
                pages.append(PageText(i, text, ocr=False))
                continue
            if not ocr_available():
                # Keep whatever little text exists; don't fail the whole PDF.
                pages.append(PageText(i, text, ocr=False))
                continue
            pix = page.get_pixmap(dpi=OCR_DPI)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            # A 300 dpi render is already large; skip the 2x upscale.
            pages.append(PageText(i, ocr_image(img, upscale=False), ocr=True))
    return pages


def clean(text: str) -> str:
    """Normalize whitespace per line, drop empty runs, keep paragraph breaks."""
    lines = [" ".join(line.split()) for line in text.replace("\r", "").split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()
