# bot/knowledge_base/context_manager.py
from __future__ import annotations

from typing import Dict, List


class ContextManager:
    """
    Сборка человеко-читаемого контекста из чанков.
    """
    def __init__(self, max_chunks: int = 6):
        self.max_chunks = max_chunks

    def build_context(self, chunks: List[Dict]) -> str:
        parts: List[str] = []
        for ch in chunks[: self.max_chunks]:
            title = ch.get("title") or "Документ"
            body = ch.get("chunk") or ""
            parts.append(f"[{title}] {body}")
        return "\n\n".join(parts)
