# bot/openai_helper.py
from __future__ import annotations

import io
import logging
from typing import List, Optional, Tuple

from openai import OpenAI
from openai.types.chat import ChatCompletion

logger = logging.getLogger(__name__)


class OpenAIHelper:
    """
    Тонкая обёртка над OpenAI SDK.

    ВАЖНО: первичный путь — Responses API (client.responses.create).
    Если оно вернёт 4xx (например, из-за несовместимого формата),
    сработает fallback на Chat Completions (client.chat.completions.create).
    """

    def __init__(
        self,
        api_key: str,
        default_model: Optional[str] = None,
        default_temperature: float = 0.2,
        image_model: Optional[str] = None,
        enable_image_generation: bool = True,
    ):
        # Инициализируем клиент и сохраняем под ОДНОВРЕМЕННО двумя именами
        # для совместимости со старым кодом:
        self._client: OpenAI = OpenAI(api_key=api_key)
        self.client: OpenAI = self._client  # alias на всякий случай

        self.default_model = default_model or "gpt-4o"
        self.default_temperature = default_temperature
        self.image_model = image_model or "gpt-image-1"
        self.enable_image_generation = enable_image_generation

    # ---------------- Модели для меню ----------------

    def list_models_for_menu(self) -> List[str]:
        """
        Возвращает список моделей для кнопок /model.
        Оставляем статический список, чтобы не зависеть от доступности /models.
        """
        return [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4o-audio-preview",
            "gpt-image-1",
            # если хотите — добавьте свои кастомные (o1, o3 и т.п.), если доступны вашему ключу
        ]

    # ---------------- Основной чат ----------------

    def _make_messages(self, user_text: str, style: str, kb_context: Optional[str]) -> List[dict]:
        sys_style = {
            "Pro": "Отвечай кратко и профессионально.",
            "Expert": "Отвечай как эксперт-практик, с конкретикой.",
            "User": "Отвечай просто и дружелюбно.",
            "CEO": "Отвечай как руководитель: по делу, кратко и по приоритетам.",
        }.get(style or "Pro", "Отвечай кратко и профессионально.")

        system_parts = [sys_style]
        if kb_context:
            system_parts.append(
                "Используй приведённые ниже выдержки из Базы знаний как основной источник. "
                "Если ответ прямо содержится в выдержках — делай ссылку на источник/страницу. "
                "Если сведений недостаточно — честно скажи, чего не хватает."
            )
            system_parts.append(kb_context)

        system_prompt = "\n\n".join(system_parts)

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

    def chat(
        self,
        user_text: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        style: str = "Pro",
        kb_context: Optional[str] = None,
    ) -> str:
        """
        Пробуем Responses API; если 4xx — делаем fallback на Chat Completions.
        """
        mdl = model or self.default_model
        temp = self.default_temperature if temperature is None else float(temperature)

        messages = self._make_messages(user_text, style, kb_context)

        # --- 1) Responses API ---
        try:
            resp = self._client.responses.create(
                model=mdl,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                # Некоторые билды SDK ожидают типы наподобие 'input_text'.
                                # Если ваш аккаунт/модель не принимает такой формат,
                                # будет 400, и мы уйдём в fallback ниже.
                                "type": "input_text",
                                "text": messages[-1]["content"],
                            }
                        ],
                    }
                ],
                temperature=temp,
            )
            # Попробуем собрать текст из output
            out_text_parts: List[str] = []
            outputs = getattr(resp, "output", None)
            if outputs:
                for item in outputs:
                    if getattr(item, "type", "") == "output_text":
                        txt = getattr(item, "content", "")
                        if txt:
                            out_text_parts.append(str(txt))
            if out_text_parts:
                return "\n".join(out_text_parts)

            # Если формат не тот — пусть идёт в fallback
            raise ValueError("Responses API returned no output_text; switching to Chat Completions fallback")

        except Exception as e:
            # Часто здесь 400: invalid value 'text' и т.п.
            logger.warning(
                "Responses.create failed (%s). Trying Chat Completions fallback...",
                getattr(e, "args", [e])[0],
            )

        # --- 2) Chat Completions (fallback) ---
        try:
            cc: ChatCompletion = self._client.chat.completions.create(
                model=mdl,
                temperature=temp,
                messages=messages,
            )
            choice = cc.choices[0]
            return choice.message.content or ""
        except Exception as e:
            logger.error("chat() failed in fallback: %s", e, exc_info=True)
            raise

    # ---------------- Web/Search заглушка ----------------

    def web_answer(self, query: str) -> Tuple[str, List[str]]:
        """
        Простейший заглушечный ответ. Если у вас есть внешний веб-поиск,
        подключите его здесь.
        """
        text = f"По запросу «{query}» нашёл краткое резюме. (Демо-режим без реальных источников.)"
        return text, []

    # ---------------- Аудио (Whisper) ----------------

    def transcribe_audio(self, audio_bytes: bytes, file_name: str = "audio.ogg") -> str:
        """
        Транскрибирует речь. Использует модель whisper-1.
        """
        try:
            with io.BytesIO(audio_bytes) as f:
                f.name = file_name
                tr = self._client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                )
            return tr.text or ""
        except Exception as e:
            logger.error("transcribe_audio failed: %s", e, exc_info=True)
            raise

    # ---------------- Изображения ----------------

    def generate_image(self, prompt: str, size: Optional[str] = None) -> Tuple[bytes, str]:
        """
        Генерация изображения.
        Возвращает (PNG bytes, использованный prompt).
        """
        if not self.enable_image_generation:
            raise RuntimeError("Image generation disabled by config")

        mdl = self.image_model or "gpt-image-1"
        try:
            res = self._client.images.generate(
                model=mdl,
                prompt=prompt,
                size=size or "1024x1024",
            )
            b64 = res.data[0].b64_json
            import base64

            return base64.b64decode(b64), prompt
        except Exception as e:
            logger.warning("Primary image model '%s' failed: %s", mdl, e, exc_info=False)
            # попробуем дефолтный
            try:
                res = self._client.images.generate(
                    model="gpt-image-1",
                    prompt=prompt,
                    size=size or "1024x1024",
                )
                b64 = res.data[0].b64_json
                import base64

                return base64.b64decode(b64), prompt
            except Exception as e2:
                logger.error("Image generation failed: %s", e2, exc_info=True)
                raise

    def describe_image(self, image_bytes: bytes) -> str:
        """
        Очень простое описание картинки. Для продакшн-качества лучше сделать
        multimodal prompt с image_url=...
        """
        return "Картинка получена. (Демо-описание изображения отключено в этой сборке.)"

    def describe_file(self, file_bytes: bytes, file_name: str) -> str:
        """
        Простейшее описание файла (заглушка).
        """
        return f"Файл «{file_name}» получен. (Анализ содержимого отключён в этой сборке.)"
