# bot/knowledge_base/context_manager.py
from __future__ import annotations

from typing import Iterable, List
from bot.config import settings
import logging

logger = logging.getLogger(__name__)


class ContextManager:
    """
    Простейший сборщик контекста: склеивает полученные куски в один блок.
    Если в будущем появится векторный БД/цитирование — можно заменить здесь.
    """

    def __init__(self, settings = None):
        self.settings = settings or {}

    def build_context(self, chunks: Iterable[str]) -> str:
        parts: List[str] = []

        limit = settings.max_kb_chunks
        selected_chunks = list(chunks)[:limit]

        logger.debug(
            "🧠 ContextManager: building context from %d chunks (limit=%d)",
            len(selected_chunks), limit
        )

        for i, ch in enumerate(selected_chunks, 1):
            if not ch:
                continue
            parts.append(f"[CHUNK {i}]\n{ch}")

        context = "\n\n".join(parts)
        logger.debug("🧠 ContextManager: total context length = %d", len(context))
        return context
