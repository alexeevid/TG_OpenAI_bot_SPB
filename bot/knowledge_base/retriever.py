# bot/knowledge_base/retriever.py
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import List

from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


@dataclass
class KBChunk:
    doc_id: str
    doc_title: str
    score: float
    page: int | None
    text: str
    source_ref: str


class KnowledgeBaseRetriever:
    """
    Поиск по БЗ:
    - если доступен PG + pgvector (URL в settings), пытаемся использовать его;
    - иначе — фолбэк: простой on-disk "инвертированный индекс" по заголовкам (stub).
    """

    def __init__(self, settings):
        self.settings = settings
        self._engine: Engine | None = None
        self._vector_ready: bool = False

        url = getattr(settings, "kb_vector_db_url", None)
        if url:
            try:
                self._engine = create_engine(url, pool_pre_ping=True)
                with self._engine.connect() as conn:
                    # простая проверка наличия функции расстояния (pgvector)
                    conn.execute(text("SELECT 1"))
                    self._vector_ready = True  # не гарантирует extension, но позволяет жить
            except Exception as e:
                logger.warning("KB Retriever: DB init failed: %s, fallback to on-disk", e)
                self._engine = None
                self._vector_ready = False
        else:
            logger.info("KB Retriever: vector DB URL not set -> fallback mode")

        # Файловый индекс-стаб для фолбэка
        self._local_index_path = os.path.abspath("./data/kb_index.json")

        # OpenAI client для эмбеддингов при vector режиме (в будущем)
        self._openai = OpenAI(api_key=settings.openai_api_key)
        self._embedding_model = getattr(settings, "kb_embedding_model", "text-embedding-3-small")

    def is_vector_store(self) -> bool:
        return bool(self._engine and self._vector_ready)

    def _fallback_retrieve(self, query: str, selected_ids: List[str], k: int) -> List[KBChunk]:
        """
        Простейший фолбэк: ищем в названиях документов (без контента).
        """
        chunks: List[KBChunk] = []
        try:
            with open(self._local_index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            files = data.get("files", {})
        except Exception:
            files = {}

        query_l = query.lower()
        for v in files.values():
            if selected_ids and v["id"] not in selected_ids:
                continue
            title = v.get("title") or v.get("path") or v.get("id")
            score = 1.0 if query_l in title.lower() else 0.1
            if score <= 0.1 and query_l not in title.lower():
                continue
            chunks.append(
                KBChunk(
                    doc_id=v["id"],
                    doc_title=title,
                    score=score,
                    page=None,
                    text=f"(фолбэк) Совпадение по названию: «{title}». Для полноты контента нужен индекс.",
                    source_ref=title,
                )
            )
        chunks.sort(key=lambda c: c.score, reverse=True)
        return chunks[:k]

    def retrieve(self, query: str, selected_ids: List[str], *, k: int = 6) -> List[KBChunk]:
        """
        На данном этапе:
        - Если есть vector БД — можно расширить до реального поиска по эмбеддингам.
          Пока возвращаем фолбэк (без эмбеддингов), чтобы не ломать UX.
        - Если векторного нет — фолбэк по заголовкам.
        """
        if self.is_vector_store():
            # TODO: Реальный pgvector-поиск по эмбеддингам (в следующей итерации).
            logger.info("KB Retriever: vector store detected, but using stub query for now (next iteration).")
            return self._fallback_retrieve(query, selected_ids, k)
        else:
            return self._fallback_retrieve(query, selected_ids, k)
