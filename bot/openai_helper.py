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
            return txt.strip()

        # Ручной разбор (на случай, если output_text отсутствует)
        chunks: List[str] = []
        for out in (getattr(resp, "output", []) or []):
            if getattr(out, "type", None) == "message":
                for part in (getattr(out, "content", []) or []):
                    if getattr(part, "type", None) == "text":
                        t = getattr(part, "text", None)
                        if t:
                            chunks.append(t)
        if chunks:
            return "\n".join(chunks).strip()

        logger.warning("Responses API returned no text content.")
        return "⚠️ Модель не вернула текст ответа."

    # ===================== ИЗОБРАЖЕНИЯ =====================

    def generate_image(self, prompt: str, model: Optional[str] = None) -> Tuple[bytes, str]:
        """
        Генерирует изображение. Возвращает (bytes_png, used_prompt).
        Точечная правка: всегда просим base64 (response_format='b64_json') и валидируем.
        Сохраняем fallback на 'dall-e-3', если primary недоступна.
        """
        primary = model or self.image_model or "gpt-image-1"
        fallbacks = ["dall-e-3"] if primary != "dall-e-3" else []

        last_err: Optional[Exception] = None

        def _call(img_model: str) -> bytes:
            res = self.client.images.generate(
                model=img_model,
                prompt=prompt,
                n=1,
                size="1024x1024",
                response_format="b64_json",  # критично для стабильности
            )
            data = res.data[0]
            b64 = getattr(data, "b64_json", None)
            if not b64:
                raise RuntimeError("Images API did not return base64 image.")
            return base64.b64decode(b64)

        # Сначала пробуем primary
        try:
            return _call(primary), prompt
        except Exception as e:
            logger.warning("Primary image model '%s' failed: %s", primary, e)
            last_err = e

        # Затем — fallback-и
        for fb in fallbacks:
            try:
                return _call(fb), prompt
            except Exception as e:
                logger.error("Fallback image model '%s' failed: %s", fb, e)
                last_err = e

        raise RuntimeError(f"Image generation failed: {last_err}")

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
