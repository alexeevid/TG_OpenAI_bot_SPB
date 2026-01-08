# app/services/document_service.py
from __future__ import annotations

import base64
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image
from pypdf import PdfReader
from docx import Document as DocxDocument
import openpyxl

log = logging.getLogger(__name__)


@dataclass
class ExtractResult:
    text: str
    info: str  # краткая диагностика: откуда извлекали, что получилось


class DocumentService:
    """
    Извлечение текста из разных типов входа.

    OCR по изображению делаем через OpenAI (Vision) — это не требует tesseract/apt-get,
    т.е. совместимо с вашим минимальным Dockerfile на Railway.
    """

    def __init__(self, openai_client, settings):
        self._openai = openai_client
        self._cfg = settings

    def _guess_mime(self, filename: str, mime: Optional[str]) -> str:
        if mime:
            return mime
        mt, _ = mimetypes.guess_type(filename or "")
        return mt or "application/octet-stream"

    def _vision_model(self) -> str:
        # Можно добавить отдельную настройку, но не обязательно.
        # По умолчанию используем текстовую модель, если она поддерживает vision.
        m = getattr(self._cfg, "openai_vision_model", None)
        if m:
            return str(m)
        return str(getattr(self._cfg, "openai_text_model", "gpt-4o-mini"))

    def extract_text(self, path: str | Path, *, filename: str = "", mime: Optional[str] = None) -> ExtractResult:
        p = Path(path)
        if not p.exists():
            return ExtractResult("", "file_not_found")

        filename = filename or p.name
        mime = self._guess_mime(filename, mime)

        try:
            if mime.startswith("image/"):
                return self._extract_image_ocr(p)

            if mime in ("application/pdf",):
                return self._extract_pdf_text(p)

            # Office / text
            ext = (p.suffix or "").lower()

            if ext == ".docx":
                return self._extract_docx(p)

            if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
                return self._extract_xlsx(p)

            if ext in (".csv", ".txt", ".md"):
                return self._extract_textfile(p)

            # fallback: попытка как текст
            return self._extract_textfile(p)

        except Exception as e:
            log.exception("DocumentService.extract_text failed: %s", e)
            return ExtractResult("", f"error:{type(e).__name__}")

    # ---------- Extractors ----------

    def _extract_textfile(self, p: Path) -> ExtractResult:
        data = p.read_bytes()
        # пробуем UTF-8, иначе cp1251
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp1251", errors="replace")
        text = text.strip()
        info = f"text:{p.suffix.lower()} chars={len(text)}"
        return ExtractResult(text, info)

    def _extract_docx(self, p: Path) -> ExtractResult:
        doc = DocxDocument(str(p))
        parts = []
        for para in doc.paragraphs:
            t = (para.text or "").strip()
            if t:
                parts.append(t)
        text = "\n".join(parts).strip()
        info = f"docx paragraphs={len(doc.paragraphs)} chars={len(text)}"
        return ExtractResult(text, info)

    def _extract_xlsx(self, p: Path) -> ExtractResult:
        wb = openpyxl.load_workbook(str(p), data_only=True)
        out = []
        for ws in wb.worksheets[:3]:  # ограничим первыми 3 листами
            out.append(f"## Sheet: {ws.title}")
            max_row = min(ws.max_row or 0, 60)
            max_col = min(ws.max_column or 0, 20)
            for r in range(1, max_row + 1):
                row_vals = []
                for c in range(1, max_col + 1):
                    v = ws.cell(r, c).value
                    if v is None:
                        row_vals.append("")
                    else:
                        s = str(v).strip()
                        row_vals.append(s)
                # схлопываем пустые строки
                if any(x for x in row_vals):
                    out.append(" | ".join(row_vals))
        text = "\n".join(out).strip()
        info = f"xlsx sheets={len(wb.worksheets)} chars={len(text)}"
        return ExtractResult(text, info)

    def _extract_pdf_text(self, p: Path) -> ExtractResult:
        reader = PdfReader(str(p))
        parts = []
        for i, page in enumerate(reader.pages[:25]):  # ограничим 25 страницами
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            t = t.strip()
            if t:
                parts.append(f"## Page {i+1}\n{t}")
        text = "\n\n".join(parts).strip()
        if text:
            return ExtractResult(text, f"pdf:text pages={min(len(reader.pages),25)} chars={len(text)}")

        # Если PDF сканированный — без OCR-конвертации страниц мы его не прочитаем
        # (в вашем Dockerfile нет poppler/ghostscript). Поэтому честно сообщаем.
        return ExtractResult(
            "",
            "pdf:no_text (likely scanned). send photo/pages as images for OCR",
        )

    def _extract_image_ocr(self, p: Path) -> ExtractResult:
        # нормализуем в PNG (уменьшаем риск проблем с форматами)
        img = Image.open(str(p))
        img = img.convert("RGB")

        # ограничим размер по длинной стороне
        max_side = 1600
        w, h = img.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)))

        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b = buf.getvalue()

        data_url = "data:image/png;base64," + base64.b64encode(b).decode("ascii")

        prompt = (
            "Считай текст с изображения (OCR). "
            "Верни ТОЛЬКО распознанный текст без комментариев. "
            "Сохраняй структуру (заголовки/таблицы/пункты) насколько возможно. "
            "Если рукописный текст — постарайся восстановить смысл. "
        )

        model = self._vision_model()

        # OpenAIClient.generate_text использует Responses API и принимает input=messages.
        # Передаём multimodal content.
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ]

        try:
            text = self._openai.generate_text(model=model, messages=messages, temperature=0.0, max_output_tokens=1500)
        except Exception as e:
            log.exception("image OCR via OpenAI failed: %s", e)
            return ExtractResult("", f"image:ocr_error:{type(e).__name__}")

        text = (text or "").strip()
        return ExtractResult(text, f"image:ocr chars={len(text)} model={model}")
