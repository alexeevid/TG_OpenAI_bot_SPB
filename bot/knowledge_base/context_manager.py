# bot/knowledge_base/context_manager.py
from __future__ import annotations
from bot.knowledge_base.retriever import KBChunk

# bot/knowledge_base/context_manager.py

from typing import Iterable, List, Optional

class ContextManager:
    # ваш __init__ оставьте как есть

    def build_context(self, chunks: Optional[Iterable]) -> Optional[str]:
        """
        Принимает список чанков (объекты/словари) и формирует компактный блок контекста.
        Поддерживает поля: text, source, page, score (если есть).
        """
        if not chunks:
            return None

        lines: List[str] = []
        for i, ch in enumerate(chunks, 1):
            # универсальный доступ к полям, чтобы не зависеть от точного типа
            text = getattr(ch, "text", None) or (ch.get("text") if isinstance(ch, dict) else None) or ""
            source = getattr(ch, "source", None) or (ch.get("source") if isinstance(ch, dict) else None) or "unknown"
            page = getattr(ch, "page", None) or (ch.get("page") if isinstance(ch, dict) else None)
            score = getattr(ch, "score", None) or (ch.get("score") if isinstance(ch, dict) else None)

            header = f"[doc: {source}"
            if page is not None:
                header += f" | p.{page}"
            if score is not None:
                try:
                    header += f" | score={float(score):.3f}"
                except Exception:
                    pass
            header += "]"

            lines.append(header)
            # ограничим каждый кусок ~800–1000 символов, чтобы не «забить» окно контекста
            snippet = (text or "").strip()
            if len(snippet) > 1000:
                snippet = snippet[:1000] + " …"
            lines.append(snippet)
            lines.append("")  # пустая строка-разделитель

        return "\n".join(lines).strip()
