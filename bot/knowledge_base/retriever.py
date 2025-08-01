from __future__ import annotations

import io
import logging
import os
import tempfile
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import yadisk  # type: ignore
except Exception:
    yadisk = None  # будет отражено в логах

try:
    from pypdf import PdfReader  # type: ignore
except Exception:
    PdfReader = None  # будет видно в логах

logger = logging.getLogger(__name__)


class KnowledgeBaseRetriever:
    """
    Упрощённый ретривер:
    - качает файлы с Я.Диска по токену;
    - извлекает текст (PDF, TXT/MD, DOCX при желании);
    - режет на простые «чанки» и возвращает список строк,
      который затем конкатенируется в промпт.
    Пароли к PDF передаются ПО ВЫЗОВУ (passwords[path] = pwd).
    Никаких переменных окружения для паролей не используется.
    """

    def __init__(self, settings):
        self.settings = settings
        self._token = getattr(settings, "yandex_disk_token", None) or getattr(settings, "yadisk_token", None)
        if not self._token:
            logger.warning("KB Retriever: Yandex token not found")
        if yadisk is None:
            logger.warning("KB Retriever: yadisk not installed, cannot download files")
        if PdfReader is None:
            logger.warning("KB Retriever: pypdf not installed — cannot extract text from PDF")

    # --------- Публичный API ---------
    def retrieve(self, query: str, selected_paths: List[str], passwords: Dict[str, str]) -> List[str]:
        """
        Возвращает список коротких выдержек (строк) из выбранных документов.
        :param query: вопрос пользователя (можно использовать для эвристик)
        :param selected_paths: ['disk:/База/file1.pdf', ...]
        :param passwords: {'disk:/База/file1.pdf': 'secret', ...} — пароли СЕССИИ
        """
        if not (self._token and yadisk):
            return []

        y = yadisk.Client(token=self._token)

        chunks: List[str] = []
        for disk_path in selected_paths:
            try:
                text = self._download_and_extract_text(y, disk_path, passwords.get(disk_path))
                if not text:
                    continue
                chunks.extend(self._naive_chunk(text, 1600, 200, max_chunks=6))
            except Exception as e:
                logger.warning("KB Retriever: failed for %s: %s", disk_path, e)

        return chunks

    # --------- Внутренние ---------
    def _download_and_extract_text(self, y, disk_path: str, password: Optional[str]) -> str:
        """
        Скачивает файл с Я.Диска во временный файл и извлекает текст.
        Если PDF зашифрован — использует переданный пароль.
        """
        # get download link
        try:
            dl = y.get_download_link(disk_path)  # type: ignore
        except Exception as e:
            logger.warning("KB Retriever: cannot get download link for %s: %s", disk_path, e)
            return ""

        # download
        with tempfile.NamedTemporaryFile(suffix=os.path.splitext(disk_path)[1] or "", delete=True) as tmp:
            try:
                y.download(dl, tmp.name)  # type: ignore
            except Exception as e:
                logger.warning("KB Retriever: download failed for %s: %s", disk_path, e)
                return ""

            # size check
            try:
                if os.path.getsize(tmp.name) == 0:
                    logger.warning("KB Retriever: empty download for %s", disk_path)
                    return ""
            except Exception:
                pass

            ext = (os.path.splitext(disk_path)[1] or "").lower()
            if ext == ".pdf":
                return self._extract_pdf(tmp.name, disk_path, password)
            elif ext in (".txt", ".md"):
                try:
                    with open(tmp.name, "r", encoding="utf-8", errors="ignore") as f:
                        return f.read()
                except Exception as e:
                    logger.warning("KB Retriever: txt/md read error for %s: %s", disk_path, e)
                    return ""
            elif ext in (".docx",):
                try:
                    from docx import Document  # lazy import
                    doc = Document(tmp.name)
                    return "\n".join(p.text for p in doc.paragraphs)
                except Exception as e:
                    logger.warning("KB Retriever: docx read error for %s: %s", disk_path, e)
                    return ""
            else:
                # другие форматы можно добавить по мере надобности
                return ""

    def _extract_pdf(self, file_path: str, disk_path: str, password: Optional[str]) -> str:
        if PdfReader is None:
            logger.warning("KB Retriever: pypdf not installed — cannot extract text from PDF")
            return ""
        try:
            with open(file_path, "rb") as f:
                reader = PdfReader(f)
                if getattr(reader, "is_encrypted", False):
                    if not password:
                        logger.warning("KB Retriever: %s encrypted, password not provided", disk_path)
                        return ""
                    # pypdf new API: decrypt returns None/1, try/catch
                    try:
                        res = reader.decrypt(password)  # type: ignore
                        # в современных pypdf возвращает int/None; проверим доступ к первой странице
                        _ = reader.pages[0]
                    except Exception:
                        logger.warning("KB Retriever: %s wrong password", disk_path)
                        return ""
                text_parts: List[str] = []
                for p in reader.pages:
                    try:
                        text_parts.append(p.extract_text() or "")
                    except Exception:
                        continue
                return "\n".join(text_parts).strip()
        except Exception as e:
            logger.warning("KB Retriever: pdf read error for %s: %s", disk_path, e)
            return ""

    @staticmethod
    def _naive_chunk(text: str, chunk_size: int, overlap: int, max_chunks: int = 8) -> List[str]:
        """
        Простейшая сегментация текста на куски фиксированной длины.
        Этого достаточно, чтобы «подлить» немного контекста в промпт.
        """
        if not text:
            return []
        text = " ".join(text.split())  # normalize spaces
        out: List[str] = []
        i = 0
        while i < len(text) and len(out) < max_chunks:
            out.append(text[i:i + chunk_size])
            i += max(1, chunk_size - overlap)
        return out
