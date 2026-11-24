"""Text extraction from answer sheets / question papers.

Supports text PDFs (PyMuPDF), plain .txt files, and - when the optional
`easyocr` package is installed - scanned PDF pages and image files (OCR).
"""
import logging
import os
import threading
from dataclasses import dataclass, field

import fitz  # PyMuPDF

logging.getLogger("pdfminer").setLevel(logging.ERROR)
log = logging.getLogger(__name__)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
MIN_TEXT_CHARS = 20  # a page with fewer characters (but with images) is treated as scanned


class OCRUnavailable(RuntimeError):
    pass


@dataclass
class ExtractResult:
    text: str = ""
    pages: int = 0
    ocr_pages: int = 0
    warnings: list = field(default_factory=list)


_reader = None
_reader_lock = threading.Lock()


def _get_ocr_reader():
    global _reader
    with _reader_lock:
        if _reader is None:
            try:
                import easyocr
            except ImportError as exc:
                raise OCRUnavailable(
                    "This looks like a scanned/photographed sheet. Install OCR support with "
                    "'pip install easyocr' and upload it again."
                ) from exc
            _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        return _reader


def _boxes_to_lines(results):
    """EasyOCR returns (bbox, text, conf) boxes; rebuild reading-order text lines."""
    items = []
    for bbox, text, _conf in results:
        ys = [p[1] for p in bbox]
        xs = [p[0] for p in bbox]
        items.append({"x": min(xs), "yc": sum(ys) / len(ys), "h": max(ys) - min(ys), "t": text})
    items.sort(key=lambda i: i["yc"])
    lines, current, line_y, line_h = [], [], None, None
    for it in items:
        if line_y is not None and abs(it["yc"] - line_y) <= 0.6 * max(line_h, 1):
            current.append(it)
        else:
            if current:
                lines.append(current)
            current, line_y, line_h = [it], it["yc"], it["h"]
    if current:
        lines.append(current)
    return "\n".join(" ".join(i["t"] for i in sorted(l, key=lambda i: i["x"])) for l in lines)


def ocr_image_bytes(data: bytes) -> str:
    reader = _get_ocr_reader()
    return _boxes_to_lines(reader.readtext(data))


class PDFProcessor:
    def __init__(self, start_page=1, ocr=True):
        self.start_page = max(start_page - 1, 0)  # 0-based
        self.ocr = ocr

    # -- public API ---------------------------------------------------------
    def extract(self, path) -> ExtractResult:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            return self._extract_pdf(path)
        if ext in IMAGE_EXTS:
            with open(path, "rb") as fh:
                text = ocr_image_bytes(fh.read())
            return ExtractResult(text=text, pages=1, ocr_pages=1)
        if ext in {".txt", ".md"}:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return ExtractResult(text=fh.read(), pages=1)
        raise ValueError(f"Unsupported file type: {ext or 'unknown'}")

    def extract_text(self, path) -> str:  # kept for backwards compatibility
        return self.extract(path).text

    # -- internals ----------------------------------------------------------
    def _extract_pdf(self, path) -> ExtractResult:
        result = ExtractResult()
        with fitz.open(path) as doc:
            if doc.needs_pass:
                raise ValueError("The PDF is password protected.")
            result.pages = len(doc)
            parts = []
            for i in range(self.start_page, len(doc)):
                page = doc[i]
                text = page.get_text("text", sort=True)
                if len(text.strip()) < MIN_TEXT_CHARS and page.get_images():
                    if self.ocr:
                        try:
                            pix = page.get_pixmap(dpi=200)
                            text = ocr_image_bytes(pix.tobytes("png"))
                            result.ocr_pages += 1
                        except OCRUnavailable as exc:
                            result.warnings.append(f"Page {i + 1} skipped: {exc}")
                    else:
                        result.warnings.append(f"Page {i + 1} looks scanned and OCR is disabled.")
                parts.append(text)
        result.text = "\n".join(parts)
        return result
