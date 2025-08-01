# bot/knowledge_base/context_manager.py
from __future__ import annotations

import logging
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)


class ContextManager:
    """
    Формирует текстовый контекст для LLM из чанков БЗ.
    Совместим с вызовом ContextManager(settings) из telegram_bot.py.
    """

    def __init__(self, settings=None):
        # Настройки можно использовать в будущем (лимиты, формат, локаль и т.д.)
        self.settings = settings
        # Базовые лимиты по умолчанию — можно вынести в settings при желании
        self.max_chars: int = getattr(settings, "kb_context_max_chars", 6000) if settings else 6000
        self.max_chunks: int = getattr(settings, "kb_context_max_chunks", 8) if settings else 8

    def build_context(self, chunks: Optional[Iterable[dict]]) -> str:
        """
        Принимает список чанков формата:
          { "text": str, "source": str, "page": Optional[int], "score": float }
        Возвращает единый текстовый блок для подсказки модели.
        """
        if not chunks:
            return ""

        # Сортируем по score по убыванию (если он есть)
        def _score(c: dict) -> float:
            try:
                return float(c.get("score", 0.0))
            except Exception:
                return 0.0

        items: List[dict] = sorted(list(chunks), key=_score, reverse=True)

        # Обрезаем до max_chunks
        items = items[: self.max_chunks]

        parts: List[str] = []
        used_chars = 0

        for i, ch in enumerate(items, 1):
            text = (ch.get("text") or "").strip()
            if not text:
                continue
            src = ch.get("source") or "unknown"
            page = ch.get("page")
            score = ch.get("score")

            header_bits = [f"[{i}] {src}"]
            if page is not None:
                header_bits.append(f"стр. {page}")
            if score is not None:
                try:
                    header_bits.append(f"score={float(score):.2f}")
                except Exception:
                    pass

            header = " | ".join(header_bits)
            block = f"{header}\n{text}\n"

            # Контроль суммарной длины
            if used_chars + len(block) > self.max_chars:
                remain = self.max_chars - used_chars
                if remain <= 0:
                    break
                # аккуратно обрежем блок
                block = block[:remain]
                parts.append(block)
                used_chars += len(block)
                break

            parts.append(block)
            used_chars += len(block)

        if not parts:
            return ""

        # Итоговый контекст, который будет добавлен в system/assistant промпт
        context = (
            "Ниже приведены выдержки из Базы знаний (цитаты и ссылки на источник):\n\n"
            + "\n".join(parts)
        )
        return context
