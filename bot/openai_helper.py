# bot/openai_helper.py
from __future__ import annotations

import base64
import io
import logging
from typing import List, Optional, Tuple

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

logger = logging.getLogger(__name__)


class OpenAIHelper:
    """
    Узел интеграции с OpenAI.
    ВАЖНО: оставляем прежние сигнатуры публичных методов, чтобы не трогать остальную логику бота.
    """

    def __init__(
        self,
        api_key: str,
        default_model: Optional[str] = None,
        image_model: Optional[str] = None,
        default_temperature: float = 0.2,
        enable_image_generation: bool = True,
    ):
        self.client = OpenAI(api_key=api_key)
        # Алиас для обратной совместимости (в коде бота встречается self._client)
        self._client = self.client

        self.default_model = default_model or "gpt-4o"
        self.image_model = image_model or "gpt-image-1"
        self.default_temperature = default_temperature
        self.enable_image_generation = enable_image_generation

        # Набор «меню» моделей — с «о» линейкой по вашему ТЗ
        self._menu_models = [
            # reasoning / o-series
            "o3-mini",
            "o3",
            "o1-mini",
            "o1",
            "o4-mini",
            "o4",
            # gpt-4o семья
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4o-audio-preview",
            "gpt-4o-audio-preview-2024-10-01",
            # универсальные малые
            "gpt-3.5-turbo",
        ]

    # ----------------- Сервисные -----------------

    def list_models_for_menu(self) -> List[str]:
        """
        Возвращает предустановленный список моделей для меню выбора.
        (Запрос к /models иногда «шумный» и не нужен для UX.)
        """
        return self._menu_models[:]

    # ----------------- Чат -----------------

    def _style_system_preamble(self, style: str) -> str:
        """
        Короткая настройка голоса/стиля. Можно расширять по ТЗ.
        """
        style = (style or "Pro").lower()
        if style in ("pro", "профессиональный", "профессиональный стиль"):
            return (
                "Ты отвечаешь кратко и по делу, указываешь допущения. "
                "Если вопрос двусмысленный — уточняешь."
            )
        if style in ("expert", "эксперт", "экспертный"):
            return (
                "Отвечай как эксперт-методолог. Добавляй ссылки на разделы стандартов, "
                "если они упомянуты в контексте."
            )
        if style in ("user", "пользовательский"):
            return "Отвечай простым языком, без жаргона."
        if style == "ceo":
            return "Отвечай как руководитель: стратегично, кратко, с фокусом на рисках и эффектах."
        return "Будь полезным, точным и кратким."

    def chat(
        self,
        user_text: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        style: str = "Pro",
        kb_context: Optional[str] = None,
    ) -> str:
        """
        Единая точка для текстового ответа.
        - Если передан kb_context — аккуратно подмешиваем его в system и отдельным сообщением '[KB]'.
        - Используем Chat Completions (надежнее, чем Responses для текста).
        """
        model_name = model or self.default_model
        temp = self._safe_temperature(temperature)

        system_parts: List[str] = [self._style_system_preamble(style)]
        if kb_context:
            system_parts.append(
                "Если в контексте '[KB]' есть сведения по вопросу — опирайся на них в первую очередь. "
                "Если в '[KB]' нет ответа, скажи об этом и продолжай как обычно."
            )
        system_prompt = "\n".join(system_parts)

        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": system_prompt}
        ]
        if kb_context:
            # добавляем KB отдельным сообщением от «assistant», чтобы дать высокий приоритет
            messages.append(
                {
                    "role": "assistant",
                    "content": f"[KB]\n{kb_context}",
                }
            )
        messages.append({"role": "user", "content": user_text})

        try:
            resp = self.client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temp,
            )
            text = (resp.choices[0].message.content or "").strip()
            return text or "Ответ пуст."
        except Exception as e:
            logger.exception("chat() failed: %s", e)
            # Небольшой fallback в «самый совместимый» формат
            try:
                resp = self._client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=temp,
                )
                text = (resp.choices[0].message.content or "").strip()
                return text or "Ответ пуст."
            except Exception as ee:
                logger.exception("chat() failed in fallback: %s", ee)
                raise

    def _safe_temperature(self, temperature: Optional[float]) -> float:
        try:
            if temperature is None:
                return self.default_temperature
            t = float(temperature)
            # Страхуемся от некорректных значений окружения
            if t < 0:
                t = 0.0
            if t > 2:
                t = 2.0
            return t
        except Exception:
            return self.default_temperature

    # ----------------- Изображения -----------------

    def generate_image(self, prompt: str, size: Optional[str]) -> Tuple[bytes, str]:
        """
        Генерация изображения. Возвращает (png_bytes, used_prompt).
        """
        if not self.enable_image_generation:
            raise RuntimeError("Image generation disabled in settings.")

        used_prompt = prompt.strip()
        try:
            size = (size or "1024x1024").strip()
            result = self.client.images.generate(
                model=self.image_model,
                prompt=used_prompt,
                size=size,
                response_format="b64_json",
            )
            b64 = result.data[0].b64_json
            png_bytes = base64.b64decode(b64)
            return png_bytes, used_prompt
        except Exception as e:
            logger.warning(
                "Primary image model '%s' failed: %s", self.image_model, e
            )
            # простой дауншифт: попробуем стандартный размер
            result = self.client.images.generate(
                model=self.image_model,
                prompt=used_prompt,
                size="1024x1024",
                response_format="b64_json",
            )
            b64 = result.data[0].b64_json
            png_bytes = base64.b64decode(b64)
            return png_bytes, used_prompt

    # ----------------- Аудио -----------------

    def transcribe_audio(self, audio_bytes: bytes) -> str:
        """
        Транскрипция аудио через whisper-1 (надежный вариант).
        """
        try:
            buf = io.BytesIO(audio_bytes)
            buf.name = "audio.ogg"
            buf.seek(0)
            tr = self.client.audio.transcriptions.create(
                model="whisper-1",
                file=buf,
                response_format="text",
            )
            return tr.strip()
        except Exception as e:
            logger.exception("transcribe_audio failed: %s", e)
            raise

    # ----------------- Описания вложений (упрощенно) -----------------

    def describe_file(self, file_bytes: bytes, filename: str) -> str:
        """
        Быстрая эвристика для описания файла (без вытаскивания текста из PDF/Office).
        Ничего не ломаем — оставляем как было по духу.
        """
        kb = len(file_bytes) / 1024.0
        return f"Файл '{filename}', ~{kb:.1f} KB. Могу использовать содержимое в качестве контекста, если его распарсить в БЗ."

    def describe_image(self, image_bytes: bytes) -> str:
        """
        Короткое описание изображения через vision (опционально можно расширить).
        """
        try:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            messages: List[ChatCompletionMessageParam] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Опиши изображение кратко."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                }
            ]
            resp = self.client.chat.completions.create(
                model=self.default_model,
                messages=messages,
                temperature=0.2,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.exception("describe_image failed: %s", e)
            return "Не удалось описать изображение."

    # ----------------- Веб (как было) -----------------

    def web_answer(self, query: str) -> Tuple[str, List[str]]:
        """
        Заглушка под вашу текущую реализацию веб-поиска (если она была).
        Возвращаем ответ и список источников (может быть пустым).
        """
        # Сохраняем совместимость: бот ожидает кортеж (answer, sources)
        # Здесь просто отвечаем, что прямого веба нет.
        return (
            "Поиск в вебе в этой сборке ограничен: я могу ответить своими словами. "
            "Если нужны ссылки — уточните запрос.",
            [],
        )
