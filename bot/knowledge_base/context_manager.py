# bot/knowledge_base/context_manager.py
from __future__ import annotations

from typing import List
from bot.knowledge_base.retriever import KBChunk


class ContextManager:
    """
    Сборка human-readable контекста для LLM.
    Здесь можно ограничивать размер (по символам/токенам) — пока простой вариант.
    """

    def __init__(self, settings):
        self.max_chars = 5000  # грубый лимит, можно сделать на базе токенайзера

    def build_context(self, chunks: List[KBChunk]) -> str:
        if not chunks:
            return ""
        parts = []
        for ch in chunks:
            src = ch.source_ref or ch.doc_title
            p = f"[Источник: {src} | score={ch.score:.2f}]\n{ch.text}\n"
            parts.append(p)
        ctx = "\n".join(parts)
        if len(ctx) > self.max_chars:
            ctx = ctx[: self.max_chars] + "\n... (контекст урезан по длине)"
        return "### Контекст из БЗ (используй для ответа)\n" + ctx
