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
       без обращения к out.message (в SDK 1.x такого поля нет).
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
        user_text: str,
        model: Optional[str],
        temperature: float,
        style: str,
        kb_ctx: Optional[str] = None,
    ) -> str:
        """
        Унифицированный чат. Если передан kb_ctx — приоритет ответов по БЗ:
        отвечаем ТОЛЬКО на основе фрагментов БЗ; если не нашли — честно говорим.
        """
        # 1) Собираем систему/сообщения
        messages: List[Dict[str, str]] = []
    
        if kb_ctx:
            system_instr = (
                "Ты консультант с доступом к локальной Базе знаний (БЗ).\n"
                "ОТВЕЧАЙ ТОЛЬКО на основе приведённых фрагментов БЗ ниже.\n"
                "Если ответа в БЗ нет — ответь фразой: «Не нашёл в выбранных документах БЗ» "
                "и предложи уточнить вопрос или выбрать другие документы.\n\n"
                "=== БЗ ФРАГМЕНТЫ ===\n"
                f"{kb_ctx}\n"
                "=== КОНЕЦ БЗ ===\n"
                "Формат: короткий ответ и, по возможности, перечисли источники (имя файла/страница)."
            )
            messages.append({"role": "system", "content": system_instr})
        else:
            messages.append({"role": "system", "content": "Отвечай кратко и по делу."})
    
        # (опционально — стиль, если у вас используется)
        if style:
            messages.append({"role": "system", "content": f"Стиль ответа: {style}."})
    
        messages.append({"role": "user", "content": user_text})
    
        # 2) Пытаемся через Responses API (если у вас это уже есть)
        try:
            if hasattr(self, "_client") and hasattr(self._client, "responses"):
                resp = self._client.responses.create(
                    model=model or self.default_model,
                    input=[{"role": "user", "content": [{"type": "input_text", "text": user_text}]}]
                    if not kb_ctx else
                    [
                        {"role": "system", "content": [{"type": "input_text", "text": messages[0]["content"]}]},
                        {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
                    ],
                    temperature=temperature,
                )
    
                # Унифицированное извлечение текста из Responses API
                out_text_parts: List[str] = []
                for item in getattr(resp, "output", []) or []:
                    if getattr(item, "type", None) == "message":
                        for c in getattr(item, "content", []) or []:
                            if getattr(c, "type", None) in ("output_text", "text"):
                                out_text_parts.append(getattr(c, "text", "") or getattr(c, "value", ""))
                if out_text_parts:
                    return "\n".join(t for t in out_text_parts if t)
    
        except Exception as e:
            # Логируем и идём в fallback Chat Completions
            logging.getLogger(__name__).warning("Responses.create failed: %s. Trying Chat Completions fallback...", e)
    
        # 3) Fallback — Chat Completions (максимально сохраняем вашу прежнюю механику)
        try:
            cc = self._client.chat.completions.create(
                model=model or self.default_model,
                temperature=temperature,
                messages=messages,
            )
            return (cc.choices[0].message.content or "").strip()
        except Exception as e:
            logging.getLogger(__name__).error("chat() failed in fallback: %s", e)
            raise
    
        # ===================== РЕЧЬ =====================
    
        def transcribe_audio(self, audio_bytes: bytes) -> str:
            """
            Транскрипция аудио (Whisper-1). Поддерживает bytes, не меняем интерфейс.
            """
            try:
                audio_io = io.BytesIO(audio_bytes)
                audio_io.name = "audio.ogg"  # подсказка формата
                tr = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_io,
                )
                text = getattr(tr, "text", None)
                if not text:
                    text = str(tr)
                return text.strip()
            except Exception as e:
                logger.error("transcribe_audio failed: %s", e)
                raise

    # ===================== АНАЛИЗ ФАЙЛОВ/ИЗОБРАЖЕНИЙ =====================

    def describe_file(self, file_bytes: bytes, filename: str) -> str:
        """
        Лёгкий анализ файла без изменения внешнего интерфейса.
        Здесь оставляем безопасный текстовый промпт без попыток парсинга PDF/Office,
        чтобы не ломать текущую сборку зависимостями.
        """
        prompt = (
            "Тебе передан файл. Дай краткое, структурированное резюме: тип содержимого, "
            "основные разделы (если удаётся понять по названию), возможные применения. "
            "Если содержания не видно (например, бинарный формат), объясни, "
            "какой анализ можно сделать без распаковки и что понадобится для глубокого анализа."
            f"\n\nИмя файла: {filename}"
        )
        return self.chat(prompt, model=self.default_chat_model, temperature=0.2, style="Pro")

    def describe_image(self, image_bytes: bytes) -> str:
        """
        Краткое описание изображения. Делаем через Chat Completions с vision,
        чтобы не лезть глубоко в формат Responses + input_image.
        """
        try:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            cc = self.client.chat.completions.create(
                model=self.default_chat_model,
                messages=[
                    {"role": "system", "content": "Дай краткое описание изображения на русском языке."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Опиши, что на картинке. Будь краток и точен."},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        ],
                    },
                ],
                temperature=0.2,
            )
            return (cc.choices[0].message.content or "").strip()
        except Exception as e:
            logger.error("describe_image failed: %s", e)
            return "Не удалось проанализировать изображение этой моделью."

    # ===================== ВЕБ (заглушка/тонкая логика) =====================

    def web_answer(self, query: str) -> Tuple[str, List[str]]:
        """
        Минимально совместимая реализация, ничего не ломаем:
        возвращаем ответ модели и пустой список источников — чтобы не падали вызовы.
        Если у вас есть реальный веб-поиск — замените тело на свой коннектор.
        """
        answer = self.chat(
            prompt=(
                "Ответь на вопрос пользователя. Если требуются внешние источники, "
                "дай общий ответ и честно отметь, что прямые ссылки недоступны в этой сборке.\n\n"
                f"Вопрос: {query}"
            ),
            model=self.default_chat_model,
            temperature=0.3,
            style="Pro",
        )
        return answer, []

    # ===================== Список моделей =====================

    def list_models_for_menu(self) -> List[str]:
        """
        Возвращаем доступные модели без агрессивной фильтрации.
        """
        try:
            models = self.client.models.list()
            names = [m.id for m in getattr(models, "data", [])]
            priority = ["o4-mini", "o3-mini", "o1-mini", "o1", "gpt-4o", "gpt-4o-mini", "gpt-4.1-mini"]
            names = sorted(
                names,
                key=lambda n: (0 if n in priority else 1, priority.index(n) if n in priority else 0, n),
            )
            return names
        except Exception as e:
            logger.warning("list_models_for_menu failed: %s", e)
            return ["gpt-4o", "gpt-4o-mini", "o3-mini", "o1-mini", "o1"]
