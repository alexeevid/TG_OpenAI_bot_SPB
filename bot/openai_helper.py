# bot/openai_helper.py
from __future__ import annotations

import io
import os
import base64
import logging
from typing import List, Optional, Tuple

from openai import OpenAI

logger = logging.getLogger(__name__)


class OpenAIHelper:
    """
    Минимально инвазивная версия помощника работы с OpenAI.

    Точечные правки:
    1) chat(): корректный разбор ответа Responses API (используем response.output_text),
       БЕЗ обращения к out.message (которого нет в SDK).
       Есть фолбэк на Chat Completions при необходимости.
    2) generate_image(): всегда запрашиваем base64 через response_format="b64_json",
       и проверяем наличие b64. Если primary-модель недоступна, пробуем fallback.
    """

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("OpenAI API key is required")

        self.client = OpenAI(api_key=api_key)

        # Дефолты, чтобы не менять поведение остального кода
        self.default_chat_model = os.getenv("OPENAI_DEFAULT_MODEL", "gpt-4o-mini")
        self.image_model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")

    # ===================== Вспомогательные =====================

    def _style_to_system_prompt(self, style: str) -> str:
        s = (style or "Pro").lower()
        if s in ("pro", "professional", "профессиональный"):
            return (
                "Отвечай как опытный профессионал: структурировано, по делу, ясно, "
                "давай конкретные рекомендации и шаги. Избегай воды."
            )
        if s in ("expert", "эксперт"):
            return (
                "Отвечай как эксперт с глубоким доменным опытом: объясняй причинно-следственные связи, "
                "приводи лучшие практики, предупреждай о рисках и ограничениях."
            )
        if s in ("user", "пользователь", "casual"):
            return (
                "Общайся просто и дружелюбно, кратко, без излишней терминологии. "
                "Если нужна детализация — уточняй."
            )
        if s in ("ceo", "руководитель"):
            return (
                "Отвечай как руководитель: кратко, по пунктам, с приоритетами и вариантами решений. "
                "Фокусируйся на эффектах для бизнеса и сроках."
            )
        # дефолт
        return (
            "Отвечай профессионально, структурировано и понятно. "
            "Если данных не хватает, уточняй вопросы."
        )

    # ===================== ТЕКСТОВЫЙ ДИАЛОГ =====================

    def chat(
        self,
        prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        style: str = "Pro",
        kb_context: Optional[str] = None,
    ) -> str:
        """
        Возвращает текстовый ответ модели.
        Точечная правка: корректный парсинг ответа Responses API (без out.message).
        """
        mdl = model or self.default_chat_model

        # Формируем system из стиля и KB-контекста
        system_parts: List[dict] = [
            {"type": "text", "text": self._style_to_system_prompt(style)}
        ]
        if kb_context:
            system_parts.append({
                "type": "text",
                "text": (
                    "Ниже приведены выдержки из базы знаний. Используй их как первоисточник фактов. "
                    "Если сведений недостаточно — скажи явно.\n\n"
                    f"{kb_context}"
                ),
            })

        # Responses API
        inputs = [
            {"role": "system", "content": system_parts},
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ]

        try:
            resp = self.client.responses.create(
                model=mdl,
                input=inputs,
                temperature=temperature,
            )
        except Exception as e:
            # Фолбэк на Chat Completions (на случай несовместимой модели)
            logger.warning("Responses.create failed (%s). Trying Chat Completions fallback...", e)
            try:
                cc = self.client.chat.completions.create(
                    model=mdl,
                    messages=[
                        {
                            "role": "system",
                            "content": "\n".join(p["text"] for p in system_parts if p.get("type") == "text"),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=temperature,
                )
                return (cc.choices[0].message.content or "").strip()
            except Exception as ee:
                logger.error("Chat Completions fallback failed: %s", ee)
                raise

        # ✅ Основной путь: безопасное извлечение текста
        txt = getattr(resp, "output_text", None)
        if txt:
            return
::contentReference[oaicite:0]{index=0}
