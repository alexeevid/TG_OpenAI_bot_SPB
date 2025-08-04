from __future__ import annotations

import logging
import os
import tempfile
from typing import Dict, List, Optional

try:
    import yadisk  # type: ignore
except Exception:
    yadisk = None

try:
    from pypdf import PdfReader  # type: ignore
except Exception:
    PdfReader = None

from bot.config import settings  # Используем параметры из настроек

logger = logging.getLogger(__name__)


class KnowledgeBaseRetriever:
    """
    Упрощённый ретривер:
      - скачивает файлы с Я.Диска по точному пути disk:/... через Client.download(path, local_path)
      - извлекает текст (PDF, TXT/MD, DOCX)
      - режет на чанки по настройкам из bot.config.settings и возвращает список строк
    Пароли к PDF передаются в параметре passwords[path] и живут только в сессии.
    """

    def __init__(self):
        self._token = settings.yandex_disk_token or getattr(settings, "yadisk_token", None)
        if not self._token:
            logger.warning("KB Retriever: Yandex token not found")
        if yadisk is None:
            logger.warning("KB Retriever: yadisk not installed, cannot download files")
        if PdfReader is None:
            logger.warning("KB Retriever: pypdf not installed — cannot extract text from PDF")

    # ------------- PUBLIC -------------
    def retrieve(self, query: str, selected_paths: List[str], passwords: Dict[str, str]) -> List[str]:
        if not (self._token and yadisk):
            logger.debug("KB Retriever: no token or yadisk missing; return empty.")
            return []

        logger.debug(
            "KB Retriever: start. query_len=%d, docs=%d",
            len(query or ""), len(selected_paths)
        )
        y = yadisk.Client(token=self._token)

        all_chunks: List[str] = []
        for disk_path in selected_paths:
            pwd_present = bool(passwords.get(disk_path))
            logger.debug("KB Retriever: process %s (pwd=%s)", disk_path, "yes" if pwd_present else "no")

            try:
                text = self._download_and_extract(y, disk_path, passwords.get(disk_path))
            except Exception as e:
                logger.warning("KB Retriever: failed for %s: %s", disk_path, e)
                continue

            if not text:
                logger.debug("KB Retriever: no text extracted from %s", disk_path)
                continue

            # Используем динамические настройки для нарезки
            chunk_size = settings.chunk_size       # по умолчанию 1600
            overlap    = settings.chunk_overlap    # по умолчанию 200
            max_chunks = settings.max_kb_chunks    # по умолчанию 6
            chunks = self._chunk(
                text,
                chunk_size=chunk_size,
                overlap=overlap,
                max_chunks=max_chunks
            )

            logger.debug(
                "KB Retriever: %s -> text_len=%d, chunks=%d",
                disk_path, len(text), len(chunks)
            )
            all_chunks.extend(chunks)

        logger.debug("KB Retriever: total chunks=%d", len(all_chunks))
        return all_chunks

    # ------------- INTERNAL -------------
    def _download_and_extract(self, y, disk_path: str, password: Optional[str]) -> str:
        """Скачивает disk_path во временный файл и извлекает текст по типу."""
        suffix = os.path.splitext(disk_path)[1].lower() or ""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
            try:
                logger.debug("KB Retriever: download %s -> %s", disk_path, tmp.name)
                y.download(disk_path, tmp.name)  # type: ignore
            except Exception as e:
                logger.warning("KB Retriever: download failed for %s: %s", disk_path, e)
                return ""

            try:
                size = os.path.getsize(tmp.name)
            except Exception:
                size = -1
            logger.debug("KB Retriever: downloaded size=%s", size)
            if size <= 0:
                return ""

            if suffix == ".pdf":
                return self._extract_pdf(tmp.name, disk_path, password)
            elif suffix in (".txt", ".md"):
                try:
                    with open(tmp.name, "r", encoding="utf-8", errors="ignore") as f:
                        return f.read()
                except Exception as e:
                    logger.warning("KB Retriever: read txt/md error %s: %s", disk_path, e)
                    return ""
            elif suffix == ".docx":
                try:
                    from docx import Document  # lazy import
                    doc = Document(tmp.name)
                    return "\n".join(p.text for p in doc.paragraphs)
                except Exception as e:
                    logger.warning("KB Retriever: read docx error %s: %s", disk_path, e)
                    return ""
            else:
                return ""

    def _extract_pdf(self, file_path: str, disk_path: str, password: Optional[str]) -> str:
        if PdfReader is None:
            logger.warning("KB Retriever: pypdf not installed — cannot extract pdf text")
            return ""
        try:
            with open(file_path, "rb") as f:
                reader = PdfReader(f)
                if getattr(reader, "is_encrypted", False):
                    if not password:
                        logger.warning("KB Retriever: %s is encrypted, no password", disk_path)
                        return ""
                    try:
                        res = reader.decrypt(password)
                        _ = reader.pages[0]
                        logger.debug("KB Retriever: %s decrypt result=%s", disk_path, res)
                    except Exception:
                        logger.warning("KB Retriever: %s wrong password", disk_path)
                        return ""

                parts: List[str] = []
                for i, page in enumerate(reader.pages):
                    try:
                        txt = page.extract_text() or ""
                        parts.append(txt)
                    except Exception as e:
                        logger.debug("KB Retriever: page %d extract failed (%s): %s", i, disk_path, e)
                return "\n".join(parts).strip()
        except Exception as e:
            logger.warning("KB Retriever: pdf open/read error %s: %s", disk_path, e)
            return ""

    @staticmethod
    def _chunk(text: str, chunk_size: int, overlap: int, max_chunks: int) -> List[str]:
        if not text:
            return []
        t = " ".join(text.split())
        out: List[str] = []
        i = 0
        while i < len(t) and len(out) < max_chunks:
            out.append(t[i:i + chunk_size])
            i += max(1, chunk_size - overlap)
        return out
