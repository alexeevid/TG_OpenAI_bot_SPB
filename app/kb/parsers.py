from __future__ import annotations

import csv
import io
import os
from PIL import Image

try:
    from docx import Document
except Exception:
    Document = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    import openpyxl
except Exception:
    openpyxl = None


def detect_ext(path: str) -> str:
    return (os.path.splitext(path)[1] or "").lower().strip(".")


def is_image_ext(ext: str) -> bool:
    return ext in {"png", "jpg", "jpeg", "webp"}


def parse_text_bytes(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")


def parse_pdf_bytes(data: bytes, password: str | None = None) -> dict:
    """Parse PDF bytes.

    Returns:
      {"text": str, "needs_password": bool}
    """
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed")

    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            if not password:
                return {"text": "", "needs_password": True}
            try:
                reader.decrypt(password)
            except Exception:
                return {"text": "", "needs_password": True}

        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return {"text": "\n".join(parts).strip(), "needs_password": False}
    except Exception as e:
        # conservative: treat as parsing error
        return {"text": "", "needs_password": False, "error": str(e)}


def parse_docx_bytes(data: bytes) -> str:
    if Document is None:
        raise RuntimeError("python-docx is not installed")
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text]
    for t in doc.tables:
        for row in t.rows:
            parts.append("\t".join(c.text or "" for c in row.cells))
    return "\n".join(parts).strip()


def parse_csv_bytes(data: bytes) -> str:
    text = parse_text_bytes(data)
    reader = csv.reader(io.StringIO(text))
    return "\n".join("\t".join(row) for row in reader)


def parse_xlsx_bytes(data: bytes) -> str:
    if openpyxl is None:
        raise RuntimeError("openpyxl is not installed")
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    out = []
    for sheet in wb.worksheets:
        out.append(f"[SHEET] {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            out.append("\t".join("" if v is None else str(v) for v in row))
    return "\n".join(out)


def parse_image_bytes_best_effort(data: bytes) -> str:
    try:
        im = Image.open(io.BytesIO(data))
        return f"[IMAGE] format={im.format} size={im.size[0]}x{im.size[1]}"
    except Exception:
        return "[IMAGE]"
