# bot/knowledge_base/retriever.py
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- Опционально: клиент Я.Диска ---
try:
    import yadisk  # type: ignore
except Exception:
    yadisk = None

# --- PDF backends: пытаемся использовать pypdf, затем PyPDF2 ---
try:
    from pypdf import PdfReader  # type: ignore
    _PDF_BACKEND = "pypdf"
except Exception:  # pragma: no cover
    try:
        from PyPDF2 import PdfReader  # type: ignore
        _PDF_BACKEND = "PyPDF2"
    except Exception:  # pragma: no cover
        PdfReader = None  # type: ignore
        _PDF_BACKEND = None


@dataclass
class TextChunk:
    """Простой контейнер для куска текста."""
    doc_path: str
    text: str
    score: float = 0.0


class KnowledgeBaseRetriever:
    """
    Минимальный ретривер:
      - скачивает выбранные документы с Я.Диска;
      - извлекает текст (PDF);
      - нарезает на куски и ранжирует по запросу;
      - возвращает топ-N кусков.
    """

    def __init__(self, settings):
        self.settings = settings

        # Корень на диске (нормализуем до "disk:/...").
        raw_root = getattr(settings, "yandex_root_path", None) or getattr(settings, "yadisk_folder", None) or "disk:/"
        self.root = self._normalize_root(raw_root)

        # Токен
        self._token = getattr(settings, "yandex_disk_token", None) or getattr(settings, "yadisk_token", None)
        if yadisk and self._token:
            try:
                self._y = yadisk.Client(token=self._token)  # type: ignore
            except Exception as e:  # pragma: no cover
                logger.warning("KB Retriever: Yandex client init failed: %s", e)
                self._y = None
        else:
            self._y = None

        # Папка для внутреннего манифеста (кэш статуса)
        self._workdir = os.getenv("KB_WORKDIR", "/app/.kb")
        try:
            os.makedirs(self._workdir, exist_ok=True)
        except Exception:
            pass
        self._manifest_path = os.path.join(self._workdir, "manifest.json")
        self._manifest = self._load_manifest()

        # Настройки нарезки/ранжирования
        self.chunk_chars = int(os.getenv("KB_CHUNK_CHARS", "1200"))
        self.max_chunks_per_doc = int(os.getenv("KB_MAX_CHUNKS_PER_DOC", "20"))
        self.top_k = int(os.getenv("KB_TOP_K", "6"))
        self.max_pdf_pages = int(os.getenv("KB_MAX_PDF_PAGES", "50"))

        if PdfReader is None:
            logger.warning("KB Retriever: pypdf/PyPDF2 not installed — cannot extract text from PDF.")

    # -------------------- Публичный API --------------------

    def retrieve(self, query: str, selected_docs: List[str]) -> List[str]:
        """
        Возвращает список текстовых фрагментов (str), отсортированный по убыванию "похожести".
        :param query: пользовательский вопрос
        :param selected_docs: абсолютные пути Я.Диска (например, 'disk:/База Знаний/file.pdf')
        """
        chunks: List[TextChunk] = []
        if not selected_docs:
            return []

        for path in selected_docs:
            try:
                data = self._download_yadisk_bytes(path)
                if not data:
                    logger.warning("KB Retriever: file download empty for %s", path)
                    continue

                text = self._extract_text(path, data)
                if not text.strip():
                    continue

                for piece in self._split_text(text, self.chunk_chars)[: self.max_chunks_per_doc]:
                    score = self._simple_score(query, piece)
                    chunks.append(TextChunk(doc_path=path, text=piece, score=score))
            except Exception as e:
                logger.debug("KB Retriever: failed for %s: %s", path, e)

        chunks.sort(key=lambda c: c.score, reverse=True)
        best = [c.text for c in chunks[: self.top_k]]
        return best

    # -------------------- Утилиты --------------------

    def _normalize_root(self, raw: str) -> str:
        # Убираем пробелы по краям
        s = (raw or "").strip()
        # Если не начинается с префикса — добавляем
        if not s.startswith("disk:/"):
            # Разрешим указание без префикса: "База Знаний" -> "disk:/База Знаний"
            s = "disk:/" + s.lstrip("/\\")
        return s

    def _load_manifest(self) -> dict:
        try:
            if os.path.exists(self._manifest_path):
                with open(self._manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        logger.warning("KB Retriever: manifest.json not found, creating empty.")
        return {}

    def _download_yadisk_bytes(self, path: str) -> bytes:
        """
        Скачивает файл из Я.Диска. Возвращает байты или b''.
        """
        if self._y is None:
            raise RuntimeError("yadisk client is not initialized")
        buf = io.BytesIO()
        self._y.download(path, buf)  # может бросать исключение при 404/403
        data = buf.getvalue()
        logger.debug("KB Retriever: downloaded %s bytes=%d", path, len(data))
        return data

    # --- Извлечение текста ---

    def _extract_text(self, path: str, data: bytes) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            return self._extract_text_from_pdf_bytes(data)
        # Можно расширить для .docx/.txt и т.п.
        if ext in (".txt", ".md"):
            try:
                return data.decode("utf-8", errors="ignore")
            except Exception:
                return data.decode("latin-1", errors="ignore")
        # Остальные форматы — пока пропускаем
        return ""

    def _extract_text_from_pdf_bytes(self, pdf_bytes: bytes) -> str:
        if PdfReader is None:
            logger.warning("KB Retriever: PDF backend not available (pypdf/PyPDF2).")
            return ""
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            parts: List[str] = []
            for i, page in enumerate(reader.pages[: self.max_pdf_pages]):
                try:
                    txt = page.extract_text() or ""
                    if txt.strip():
                        parts.append(txt)
                except Exception as e:
                    logger.debug("KB Retriever: PDF page %s extract failed: %s", i, e)
            text = "\n".join(parts)
            logger.debug("KB Retriever: PDF text extracted via %s, chars=%d", _PDF_BACKEND, len(text))
            return text
        except Exception as e:
            logger.warning("KB Retriever: PDF extract failed: %s", e)
            return ""

    # --- Пост-обработка текста ---

    def _split_text(self, text: str, chunk_size: int) -> List[str]:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        chunks: List[str] = []
        start = 0
        n = len(text)
        while start < n:
            end = min(start + chunk_size, n)
            # мягко переносим по ближайшей точке/переводу строки
            pivot = text.rfind(". ", start, end)
            if pivot == -1:
                pivot = text.rfind("\n", start, end)
            if pivot == -1 or pivot <= start + chunk_size * 0.5:
                pivot = end
            chunks.append(text[start:pivot].strip())
            start = pivot
        return [c for c in chunks if c]

    def _simple_score(self, query: str, chunk: str) -> float:
        """Очень грубая оценка по пересечению токенов."""
        q = set(re.findall(r"\w+", query.lower()))
        c = set(re.findall(r"\w+", chunk.lower()))
        if not q or not c:
            return 0.0
        inter = len(q & c)
        return inter / (1 + abs(len(c) - len(q)))
