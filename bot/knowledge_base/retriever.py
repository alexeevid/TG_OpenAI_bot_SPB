# bot/knowledge_base/retriever.py
from __future__ import annotations

import logging
from typing import Any, Dict, List

try:
    from bot.settings import Settings  # type: ignore
except Exception:  # pragma: no cover
    class Settings:
        pass

logger = logging.getLogger(__name__)


class KnowledgeBaseRetriever:
    """
    Заглушка RAG-извлечения.
    Сейчас — простейший on-disk индекс со строковым поиском (пустой),
    чтобы бот не падал. Позже можно подключить pgvector/FAISS/HNSW.
    """
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self._disk_index: List[Dict[str, Any]] = []  # [{doc_id,title,path,chunk}, ...]

    def retrieve(self, query: str, doc_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Вернуть чанки вида: [{title, path, chunk, score}, ...]
        Сейчас — поиск по подстроке.
        """
        q = (query or "").lower()
        out: List[Dict[str, Any]] = []
        for rec in self._disk_index:
            if doc_ids and rec.get("doc_id") not in doc_ids:
                continue
            if q in (rec.get("chunk") or "").lower():
                out.append({
                    "title": rec.get("title") or "Документ",
                    "path": rec.get("path"),
                    "chunk": rec.get("chunk"),
                    "score": 0.5,
                })
            if len(out) >= 8:
                break
        return out
