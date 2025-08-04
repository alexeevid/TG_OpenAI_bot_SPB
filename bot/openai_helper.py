# bot/openai_helper.py
from __future__ import annotations

import base64
import logging
import os
from typing import List, Optional, Tuple

from openai import OpenAI, APIError, BadRequestError  # type: ignore

logger = logging.getLogger(__name__)


class OpenAIHelper:
    """
    Обёртка над OpenAI SDK с единым интерфейсом для бота.
    Основной сценарий — Chat Completions; Responses API намеренно не используем
    как primary, чтобы избежать ошибок несовместимости.
    """

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("OpenAI API key is required")
        # Клиент v1 (openai>=1.30.0)
        self._client = OpenAI(api_key=api_key)

        # Базовые модели по умолчанию
        self.default_chat_model = os.getenv("OPENAI_MODEL", "gpt-4o")
        self.default_image_model = os.getenv("IMAGE_MODEL", "gpt-image-1")
        self.default_embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

        # Белый список моделей для меню (по желанию через переменную окружения)
        # Пример: "gpt-4o,gpt-4o-mini,o4-mini"
        self._whitelist_env = os.getenv("ALLOWED_MODELS_WHITELIST", "")

    # ---------------------------------------------------------------------
    # Публичный API, которым пользуется Telegram-бот
    # ---------------------------------------------------------------------

    def list_models_for_menu(self) -> List[str]:
        """
        Короткий список моделей для меню выбора в боте.
        Если задан ALLOWED_MODELS_WHITELIST — берём его.
        Иначе возвращаем безопасный набор.
        """
        if self._whitelist_env.strip():
            items = [m.strip() for m in self._whitelist_env.split(",") if m.strip()]
            return items or [self.default_chat_model]

        return [
            "gpt-4o",
            "gpt-4o-mini",
            "o4-mini",
            "o4",
        ]

    def chat(
        self,
        user_text: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        style: str = "Pro",
        kb_ctx: Optional[str] = None,
    ) -> str:
        """
        Унифицированный чат-вызов.
        Если передан kb_ctx — добавляем контекст отдельным сообщением, чтобы повысить релевантность.
        """
        chat_model = model or self.default_chat_model

        if kb_ctx:
            messages = [
                {
                    "role": "system",
                    "content": "Ты помощник, который отвечает на вопросы пользователя, используя нижеуказанный контекст из базы знаний. Если в контексте нет ответа, честно скажи, что не знаешь."
                },
                {
                    "role": "user",
                    "content": f"Контекст:\n{kb_ctx}"
                },
                {
                    "role": "user",
                    "content": user_text
                },
            ]
        else:
            system_msg = [
                "You are a helpful assistant.",
                f"Answer concisely with a {style} tone.",
            ]
            system_text = "\n".join(system_msg)
            messages = [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ]

        # Включаем логирование промпта/messages для отладки
        logger.debug("PROMPT to OpenAI:\n%s", messages)
        # Если хочешь видеть сразу — можешь раскомментировать:
        # print("PROMPT to OpenAI:\n", messages)

        try:
            resp = self._client.chat.completions.create(
                model=chat_model,
                messages=messages,
                temperature=float(temperature or 0.2),
            )
            out = (resp.choices[0].message.content or "").strip()
            if out:
                return out
        except BadRequestError as e:
            logger.error("chat() BadRequest: %s", e)
        except APIError as e:
            logger.error("chat() APIError: %s", e)
        except Exception as e:  # pragma: no cover
            logger.error("chat() failed: %s", e)

        return "Извините, не удалось получить ответ от модели."

    # --- Изображения ---

    def generate_image(self, prompt: str, size: Optional[str] = None) -> Tuple[bytes, str]:
        """
        Генерирует изображение через Images API.
        Возвращает кортеж (raw_png_bytes, used_prompt).
        """
        model = self.default_image_model
        img_size = size or "1024x1024"

        try:
            res = self._client.images.generate(
                model=model,
                prompt=prompt,
                size=img_size,
            )
            b64 = res.data[0].b64_json
            png_bytes = base64.b64decode(b64) if b64 else b""
            return png_bytes, prompt
        except Exception as e:
            logger.error("generate_image() failed: %s", e)
            raise

    # --- Аудио ---

    def transcribe_audio(self, audio_bytes: bytes) -> str:
        """
        Транскрипция через Whisper.
        """
        try:
            # Сохраняем во временный файл (httpx в SDK ожидает file-like)
            tmp = "/tmp/voice.ogg"
            with open(tmp, "wb") as f:
                f.write(audio_bytes)

            with open(tmp, "rb") as f:
                tr = self._client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                )
            text = getattr(tr, "text", "") or ""
            return text.strip()
        except Exception as e:
            logger.error("transcribe_audio() failed: %s", e)
            raise

    # --- Простые описатели для вложений ---

    def describe_file(self, file_bytes: bytes, filename: str) -> str:
        """
        Очень простой «описатель» файла без реального vision.
        Для PDF/текста можно добавить локальные эвристики.
        """
        name = filename or "file"
        size_kb = len(file_bytes) // 1024
        return f"Файл: {name}, размер ~{size_kb} KB. Поддержка глубокой семантической выжимки не включена."

    def describe_image(self, image_bytes: bytes) -> str:
        size_kb = len(image_bytes) // 1024
        return f"Изображение получено ({size_kb} KB). Анализ изображений в этой сборке минимален."

    # --- Веб-поиск (заглушка) ---

    def web_answer(self, query: str) -> Tuple[str, List[str]]:
        """
        Заглушка. При необходимости подключим внешний сервис/инструмент.
        """
        answer = (
            "В этой сборке веб-поиск отключён. "
            "Могу ответить на основании вашего контекста/БЗ, если он включён."
        )
        return answer, []
