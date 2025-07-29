from __future__ import annotations

from typing import List, Dict

class ContextManager:
    """
    Склеивает найденные фрагменты в строку контекста для передачи в LLM.
    """

    def __init__(self, settings) -> None:
        self.settings = settings

    def build_context(self, chunks: List[Dict]) -> str:
        parts = []
        for ch in chunks or []:
            txt = ch.get("text") or ch.get("content") or ""
            if txt:
                parts.append(txt)
        return "\n---\n".join(parts)
