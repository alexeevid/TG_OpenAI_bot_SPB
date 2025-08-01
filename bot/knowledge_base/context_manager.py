# bot/knowledge_base/context_manager.py
from __future__ import annotations

from typing import Iterable, List


class ContextManager:
    """
    Простейший сборщик контекста: склеивает полученные куски в один блок.
    Если в будущем появится векторный БД/цитирование — можно заменить здесь.
    """

    def __init__(self, settings = None):
        self.settings = settings

    def build_context(self, chunks: Iterable[str]) -> str:
        parts: List[str] = []
        for i, ch in enumerate(chunks, 1):
            if not ch:
                continue
            parts.append(f"[CHUNK {i}]\n{ch}")
        return "\n\n".join(parts)
