# bot/knowledge_base/retriever.py
from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine, create_engine

logger = logging.getLogger(__name__)

DEFAULT_INDEX_DIR = "/data/kb"
DEFAULT_MANIFEST = "manifest.json"


class KnowledgeBaseRetriever:
    """
    Унифицированный доступ к списку документов:
      - если доступна БД: берём из таблицы documents (title/name)
      - иначе читаем manifest.json, который пишет Indexer
    Метод retrieve() оставлен как есть у вас (или заготовка, если его нет).
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.index_dir = getattr(settings, "kb_index_dir", None) or DEFAULT_INDEX_DIR
        self.manifest_path = os.path.join(self.index_dir, DEFAULT_MANIFEST)

        # Инициализируем БД, если есть URL
        self.engine: Optional[Engine] = None
        db_url = getattr(settings, "database_url", None) or getattr(settings, "postgres_url", None)
        if db_url:
            try:
                norm = db_url.replace("postgres://", "postgresql+psycopg2://")
                self.engine = create_engine(norm, pool_pre_ping=True)
            except Exception as e:
                logger.warning("KB Retriever: DB init failed (%s), fallback to manifest.json", e)
                self.engine = None

    # ---------- публичный API ----------
    def list_documents(self) -> List[str]:
        """
        Возвращает список имён документов для интерфейса выбора.
        Приоритет: БД -> manifest.json -> []
        """
        # 1) БД
        if self.engine is not None:
            try:
                with self.engine.connect() as conn:
                    # подгоните имена столбцов/таблицы под свою схему при необходимости
                    rows = conn.execute(text("SELECT COALESCE(title, name) AS t FROM documents ORDER BY t")).fetchall()
                    names = [r[0] for r in rows if r[0]]
                    if names:
                        return names
            except Exception as e:
                logger.warning("KB Retriever: list_documents DB failed: %s", e)

        # 2) manifest.json
        try:
            if os.path.exists(self.manifest_path):
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f).get("documents", [])
                names = [d.get("name") or d.get("path") for d in data]
                return sorted(set(n for n in names if n))
        except Exception as e:
            logger.warning("KB Retriever: manifest read failed: %s", e)

        return []

    def retrieve(self, query: str, selected_docs: List[str]):
        """
        Возвращает список «чанков» по запросу и выбранным документам.
        Здесь оставьте вашу текущую векторную/классическую реализацию.
        Если у вас её пока нет — можно вернуть пустой список, тогда чат пойдёт без KB-контекста.
        """
        # ПРИМЕР-заглушка:
        return []
