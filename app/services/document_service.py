# app/services/document_service.py
from __future__ import annotations

import base64
import csv
import io
import logging
import mimetypes
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
from PIL import Image
from pypdf import PdfReader
from docx import Document as DocxDocument
import openpyxl
from bs4 import BeautifulSoup
from pptx import Presentation

log = logging.getLogger(__name__)


@dataclass
class ExtractResult:
    text: str
    info: str
    warnings: List[str]


class DocumentService:
    """
    Единый сервис извлечения текста/содержимого из файлов + OCR + нормализация/сжатие.

    Покрывает:
    - изображения: OCR через OpenAI Vision
    - PDF: текстовый (pypdf) + сканированный (PyMuPDF render -> OCR)
    - DOCX: python-docx
    - XLSX: openpyxl (табличный вывод)
    - CSV/TXT/MD: чтение
    - PPTX: python-pptx
    - HTML: bs4
    - ZIP: распаковка и обработка нескольких файлов с лимитами
    - большие документы: нормализация и при необходимости LLM-сжатие

    Важно: не требует apt-get (совместимо с вашим минимальным Dockerfile).
    """

    def __init__(self, openai_client, settings):
        self._openai = openai_client
        self._cfg = settings

        # Лимиты (без изменений settings — используем безопасные дефолты)
        self.max_pdf_pages = int(getattr(settings, "max_pdf_pages", 12))
        self.max_zip_files = int(getattr(settings, "max_zip_files", 12))
        self.max_zip_total_mb = int(getattr(settings, "max_zip_total_mb", 25))
        self.max_table_rows = int(getattr(settings, "max_table_rows", 60))
        self.max_table_cols = int(getattr(settings, "max_table_cols", 20))

        # Сжатие
        self.max_chars_before_compress = int(getattr(settings, "max_chars_before_compress", 18000))
        self.target_chars_after_compress = int(getattr(settings, "target_chars_after_compress", 9000))

        # OCR
        self.max_image_side = int(getattr(settings, "ocr_max_image_side", 1600))

    # ---------------- public ----------------

    def extract_text(self, path: str | Path, *, filename: str = "", mime: Optional[str] = None) -> ExtractResult:
        p = Path(path)
        if not p.exists():
            return ExtractResult("", "file_not_found", ["file_not_found"])

        filename = filename or p.name
        mime = self._guess_mime(filename, mime)

        warnings: List[str] = []

        try:
            # ZIP first
            if mime in ("application/zip", "application/x-zip-compressed") or p.suffix.lower() == ".zip":
                res = self._extract_zip(p)
                return self._postprocess(res)

            # Images
            if mime.startswith("image/"):
                res = self._extract_image_ocr(p)
                return self._postprocess(res)

            # PDF
            if mime == "application/pdf" or p.suffix.lower() == ".pdf":
                res = self._extract_pdf(p)
                return self._postprocess(res)

            # Office/text/html/pptx
            ext = p.suffix.lower()

            if ext == ".docx":
                res = self._extract_docx(p)
                return self._postprocess(res)

            if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
                res = self._extract_xlsx(p)
                return self._postprocess(res)

            if ext == ".pptx":
                res = self._extract_pptx(p)
                return self._postprocess(res)

            if ext in (".html", ".htm"):
                res = self._extract_html(p)
                return self._postprocess(res)

            if ext in (".csv", ".txt", ".md"):
                res = self._extract_textfile(p)
                return self._postprocess(res)

            # fallback: try as text
            res = self._extract_textfile(p)
            return self._postprocess(res)

        except Exception as e:
            log.exception("DocumentService.extract_text failed: %s", e)
            return ExtractResult("", f"error:{type(e).__name__}", [f"exception:{type(e).__name__}"])

    # ---------------- postprocess (3.5) ----------------

    def _postprocess(self, res: ExtractResult) -> ExtractResult:
        """
        3.5: стабилизация больших документов:
        - нормализация (убрать мусор/дубли/пустоты)
        - если слишком большой — LLM-сжатие до управляемого размера
        """
        text = (res.text or "").strip()
        if not text:
            return res

        text = self._normalize_text(text)
        if len(text) <= self.max_chars_before_compress:
            return ExtractResult(text=text, info=res.info, warnings=res.warnings)

        # LLM-сжатие: сохраняем структуру и факты
        compressed = self._compress_with_llm(text, target_chars=self.target_chars_after_compress)
        if compressed:
            res.warnings.append(f"compressed:{len(text)}->{len(compressed)}")
            return ExtractResult(text=compressed, info=res.info + " +compressed", warnings=res.warnings)

        # fallback: грубое обрезание (если LLM недоступен)
        cut = text[: self.target_chars_after_compress].rstrip() + "\n...\n[TRUNCATED]"
        res.warnings.append("compressed:fallback_truncate")
        return ExtractResult(text=cut, info=res.info + " +truncated", warnings=res.warnings)

    def _normalize_text(self, text: str) -> str:
        # 1) unify line endings
        t = text.replace("\r\n", "\n").replace("\r", "\n")

        # 2) remove excessive empty lines
        t = re.sub(r"\n{4,}", "\n\n\n", t)

        # 3) drop obvious duplicate consecutive lines
        lines = [ln.rstrip() for ln in t.split("\n")]
        out: List[str] = []
        prev = None
        dup_count = 0
        for ln in lines:
            if prev is not None and ln == prev and ln.strip():
                dup_count += 1
                if dup_count <= 1:
                    # keep one duplicate max
                    out.append(ln)
                continue
            dup_count = 0
            out.append(ln)
            prev = ln
        t = "\n".join(out).strip()

        # 4) collapse huge whitespace blocks
        t = re.sub(r"[ \t]{3,}", "  ", t)

        return t

    def _compress_with_llm(self, text: str, *, target_chars: int) -> str:
        """
        Сжать большой текст до target_chars с сохранением:
        - структуры разделов
        - таблиц/перечней (если есть)
        - ключевых цифр/условий
        """
        model = self._text_model_for_compress()

        prompt = (
            "Сожми текст документа, сохранив смысл, структуру и ключевые факты.\n"
            "Требования:\n"
            f"- Итоговый объём: примерно до {target_chars} символов.\n"
            "- Сохраняй заголовки/разделы.\n"
            "- Если есть таблицы — оставь их в текстовом виде, можно укоротить строки.\n"
            "- Сохраняй цифры, сроки, критерии, формулировки требований.\n"
            "- Убери повторы и воду.\n"
            "Верни ТОЛЬКО сжатый текст без комментариев.\n"
        )

        # режем вход, чтобы не убить токены: берём начало+конец
        if len(text) > 60000:
            head = text[:30000]
            tail = text[-20000:]
            text_in = head + "\n...\n[SKIPPED]\n...\n" + tail
        else:
            text_in = text

        messages = [
            {"role": "user", "content": prompt + "\n\n---\nТЕКСТ:\n" + text_in + "\n---\n"},
        ]
        try:
            out = self._openai.generate_text(
                model=model,
                messages=messages,
                temperature=0.0,
                max_output_tokens=int(getattr(self._cfg, "openai_max_output_tokens", 1800) or 1800),
                reasoning_effort=getattr(self._cfg, "openai_reasoning_effort", None),
            )
            return (out or "").strip()
        except Exception as e:
            log.warning("LLM compress failed: %s", e)
            return ""

    def _text_model_for_compress(self) -> str:
        m = getattr(self._cfg, "document_compress_model", None)
        if m:
            return str(m)
        return str(getattr(self._cfg, "openai_text_model", "gpt-4o-mini"))

    # ---------------- extractors ----------------

    def _guess_mime(self, filename: str, mime: Optional[str]) -> str:
        if mime:
            return mime
        mt, _ = mimetypes.guess_type(filename or "")
        return mt or "application/octet-stream"

    def _vision_model(self) -> str:
        m = getattr(self._cfg, "openai_vision_model", None)
        if m:
            return str(m)
        return str(getattr(self._cfg, "openai_text_model", "gpt-4o-mini"))

    def _extract_textfile(self, p: Path) -> ExtractResult:
        data = p.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp1251", errors="replace")
        text = text.strip()
        return ExtractResult(text, f"text:{p.suffix.lower()} chars={len(text)}", [])

    def _extract_docx(self, p: Path) -> ExtractResult:
        doc = DocxDocument(str(p))
        parts: List[str] = []
        for para in doc.paragraphs:
            t = (para.text or "").strip()
            if t:
                parts.append(t)
        text = "\n".join(parts).strip()
        return ExtractResult(text, f"docx paragraphs={len(doc.paragraphs)} chars={len(text)}", [])

    def _extract_xlsx(self, p: Path) -> ExtractResult:
        wb = openpyxl.load_workbook(str(p), data_only=True)
        out: List[str] = []
        for ws in wb.worksheets[:3]:
            out.append(f"## Sheet: {ws.title}")
            max_row = min(ws.max_row or 0, self.max_table_rows)
            max_col = min(ws.max_column or 0, self.max_table_cols)

            # попробуем найти “первую непустую строку” как шапку
            for r in range(1, max_row + 1):
                row_vals: List[str] = []
                for c in range(1, max_col + 1):
                    v = ws.cell(r, c).value
                    row_vals.append("" if v is None else str(v).strip())
                if not any(x for x in row_vals):
                    continue
                out.append(" | ".join(row_vals))

        text = "\n".join(out).strip()
        return ExtractResult(text, f"xlsx sheets={len(wb.worksheets)} chars={len(text)}", [])

    def _extract_pptx(self, p: Path) -> ExtractResult:
        pres = Presentation(str(p))
        out: List[str] = []
        for i, slide in enumerate(pres.slides[:30], start=1):
            out.append(f"## Slide {i}")
            for shape in slide.shapes:
                txt = getattr(shape, "text", None)
                if txt:
                    t = str(txt).strip()
                    if t:
                        out.append(t)
        text = "\n".join(out).strip()
        return ExtractResult(text, f"pptx slides={min(len(pres.slides),30)} chars={len(text)}", [])

    def _extract_html(self, p: Path) -> ExtractResult:
        data = p.read_bytes()
        try:
            html = data.decode("utf-8")
        except UnicodeDecodeError:
            html = data.decode("cp1251", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        # убираем скрипты/стили
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text("\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return ExtractResult(text, f"html chars={len(text)}", [])

    def _extract_pdf(self, p: Path) -> ExtractResult:
        # 1) пробуем вытащить текстовый PDF через pypdf
        try:
            reader = PdfReader(str(p))
            parts: List[str] = []
            for i, page in enumerate(reader.pages[: self.max_pdf_pages]):
                t = ""
                try:
                    t = (page.extract_text() or "").strip()
                except Exception:
                    t = ""
                if t:
                    parts.append(f"## Page {i+1}\n{t}")
            text = "\n\n".join(parts).strip()
            if text:
                return ExtractResult(
                    text,
                    f"pdf:text pages={min(len(reader.pages),self.max_pdf_pages)} chars={len(text)}",
                    [],
                )
        except Exception as e:
            log.warning("pypdf extract failed, will try render OCR: %s", e)

        # 2) если текста нет — рендерим страницы и делаем OCR (3.1)
        return self._extract_pdf_render_ocr(p)

    def _extract_pdf_render_ocr(self, p: Path) -> ExtractResult:
        doc = fitz.open(str(p))
        n_pages = min(doc.page_count, self.max_pdf_pages)
        warnings: List[str] = []
        if doc.page_count > n_pages:
            warnings.append(f"pdf_pages_limited:{doc.page_count}->{n_pages}")

        out_parts: List[str] = []
        for i in range(n_pages):
            page = doc.load_page(i)
            # scale=2 для читаемости OCR
            mat = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_bytes = pix.tobytes("png")
            ocr_text = self._ocr_image_bytes(img_bytes)
            ocr_text = (ocr_text or "").strip()
            if ocr_text:
                out_parts.append(f"## Page {i+1}\n{ocr_text}")

        text = "\n\n".join(out_parts).strip()
        if not text:
            warnings.append("pdf_ocr_empty")
        return ExtractResult(text, f"pdf:render_ocr pages={n_pages} chars={len(text)}", warnings)

    def _extract_zip(self, p: Path) -> ExtractResult:
        warnings: List[str] = []
        total_bytes = p.stat().st_size
        if total_bytes > self.max_zip_total_mb * 1024 * 1024:
            warnings.append(f"zip_too_large:{total_bytes}")

        out_parts: List[str] = []
        count = 0
        total_unpacked = 0

        with zipfile.ZipFile(str(p), "r") as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            # ограничим кол-во файлов
            if len(names) > self.max_zip_files:
                warnings.append(f"zip_files_limited:{len(names)}->{self.max_zip_files}")
                names = names[: self.max_zip_files]

            for name in names:
                count += 1
                # защита от zip-slip
                if ".." in Path(name).parts:
                    warnings.append(f"zip_skip_unsafe:{name}")
                    continue

                try:
                    data = z.read(name)
                except Exception:
                    warnings.append(f"zip_read_failed:{name}")
                    continue

                total_unpacked += len(data)
                if total_unpacked > self.max_zip_total_mb * 1024 * 1024:
                    warnings.append("zip_unpacked_limit_reached")
                    break

                # сохраняем во временный файл, чтобы переиспользовать общую логику
                tmp = Path("/tmp") / f"zip_{os.getpid()}_{count}_{Path(name).name}"
                try:
                    tmp.write_bytes(data)
                    child = self.extract_text(tmp, filename=Path(name).name, mime=None)
                    if child.text:
                        out_parts.append(f"# File: {name}\n{child.text}")
                    else:
                        warnings.append(f"zip_child_empty:{name}")
                finally:
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass

        text = "\n\n".join(out_parts).strip()
        return ExtractResult(text, f"zip files={count} chars={len(text)}", warnings)

    # ---------------- OCR helpers ----------------

    def _ocr_image_bytes(self, img_bytes: bytes) -> str:
        data_url = "data:image/png;base64," + base64.b64encode(img_bytes).decode("ascii")
        prompt = (
            "Считай текст с изображения (OCR).\n"
            "Верни ТОЛЬКО распознанный текст без комментариев.\n"
            "Сохраняй структуру (пункты, таблицы, заголовки) насколько возможно.\n"
            "Если рукописный текст — постарайся восстановить смысл.\n"
        )
        model = self._vision_model()
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
            out = self._openai.generate_text(
                model=model,
                messages=messages,
                temperature=0.0,
                max_output_tokens=1600,
                reasoning_effort=getattr(self._cfg, "openai_reasoning_effort", None),
            )
            return (out or "").strip()
        except Exception as e:
            log.warning("OCR via OpenAI failed: %s", e)
            return ""

    def _extract_image_ocr(self, p: Path) -> ExtractResult:
        img = Image.open(str(p)).convert("RGB")
        w, h = img.size
        scale = min(1.0, self.max_image_side / max(w, h))
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        text = self._ocr_image_bytes(buf.getvalue())
        return ExtractResult(text, f"image:ocr chars={len(text)}", [])
