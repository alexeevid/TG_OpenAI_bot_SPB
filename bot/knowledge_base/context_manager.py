# bot/knowledge_base/context_manager.py
from __future__ import annotations

from typing import List, Optional
from .retriever import KBChunk


class ContextManager:
    """
    Собирает KB_CONTEXT для промпта модели и форматирует список источников.
    """

    def __init__(self, max_chars: int = 6000):
        self.max_chars = max_chars

    def build_context(self, chunks: List[KBChunk]) -> str:
        parts = []
        used = 0
        for ch in chunks:
            block = f"[TITLE: {ch.meta.get('title','')}] [DOC: {ch.doc_id}] [SCORE: {ch.score:.3f}]\n{ch.snippet}\n"
            if used + len(block) > self.max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n---\n".join(parts)

    def build_sources_footer(self, chunks: List[KBChunk]) -> str:
        if not chunks:
            return ""
        seen = set()
        lines = []
        for ch in chunks[:8]:
            key = (ch.doc_id, ch.meta.get("title", ""))
            if key in seen:
                continue
            seen.add(key)
            title = ch.meta.get("title") or ch.doc_id
            lines.append(f"• {title}")
        if not lines:
            return ""
        return "\n\nИсточники:\n" + "\n".join(lines)
