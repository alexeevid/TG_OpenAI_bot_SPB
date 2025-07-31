# bot/knowledge_base/retriever.py
from __future__ import annotations

import os
import logging
from typing import List, Dict, Any, Optional

import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from bot.settings import Settings
from bot.db.utils import normalize_db_url

logger = logging.getLogger(__name__)


class KnowledgeBaseRetriever:
    """
    RAG-извлечение:
      - Если доступна БД + pgvector — используем её (mode='db')
      - Иначе — fallback на on-disk (mode='on-disk'), для примера — по подстроке.
    """
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.engine = None
        self.mode = "on-disk"  # "db" | "on-disk"
        self.has_pgvector = False

        raw_url = (
            getattr(self.settings, "database_url", None)
            or getattr(self.settings, "postgres_url", None)
            or os.getenv("DATABASE_URL")
            or os.getenv("POSTGRES_URL")
        )
        db_url = normalize_db_url(raw_url)

        try:
            if db_url:
                self.engine = create_engine(db_url, pool_pre_ping=True)
                with self.engine.connect() as conn:
                    res = conn.execute(
                        text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector');")
                    ).scalar()
                    self.has_pgvector = bool(res)
                if self.has_pgvector:
                    self.mode = "db"
                    logger.info("KB Retriever: using DB+pgvector")
                else:
                    logger.warning("KB Retriever: pgvector extension not found, fallback to on-disk")
                    self.engine = None
                    self.mode = "on-disk"
            else:
                logger.warning("KB Retriever: DB URL not set, using on-disk fallback")
        except Exception as e:
            logger.warning("KB Retriever: DB init failed: %s, fallback to on-disk", e)
            self.engine = None
            self.mode = "on-disk"

        # Простейший on-disk индекс (пример структуры)
        self._disk_index: List[Dict[str, Any]] = []  # [{doc_id, title, path, chunk, vector: np.ndarray}, ...]

    def retrieve(self, query: str, doc_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Возвращает список чанков: [{title, path, chunk, score}, ...]
        Реальная DB-ветка зависит от вашей схемы БД (таблицы embeddings/docs).
        Здесь показан каркас.
        """
        if self.mode == "db" and self.engine and self.has_pgvector:
            try:
                # TODO: замените на реальный запрос с использованием векторов:
                # 1) получить эмбеддинг запроса (внешне, например, через OpenAI embeddings)
                # 2) cosine_distance <-> ivfflat/hnsw оператор по pgvector
                # 3) фильтр по doc_ids
                # Ниже — заглушка:
                return []
            except SQLAlchemyError as e:
                logger.warning("DB retrieve failed: %s; fallback to on-disk", e)

        # on-disk — наивный полнотекст по подстроке (поменяйте на косинус/Faiss/hnswlib при желании)
        q = (query or "").lower()
        out: List[Dict[str, Any]] = []
        for rec in self._disk_index:
            if doc_ids and rec.get("doc_id") not in doc_ids:
                continue
            if q in rec.get("chunk", "").lower():
                out.append({
                    "title": rec.get("title"),
                    "path": rec.get("path"),
                    "chunk": rec.get("chunk"),
                    "score": 0.5,
                })
            if len(out) >= 8:
                break
        return out
