# bot/openai_helper.py
from __future__ import annotations

import base64
import io
import logging
import re
from typing import List, Tuple, Optional

import requests
from openai import OpenAI, APIError

logger = logging.getLogger(__name__)


class OpenAIHelper:
    """
    Унифицированная обвязка OpenAI API под наше ТЗ.
    """

    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)

        # Кеш для списка моделей на время жизни процесса
        self._models_cache: Optional[List[Tuple[str, bool]]] = None  # (name, locked)

        # Настройки по умолчанию (могут быть переопределены settings в telegram_bot)
        self._default_chat_model = "gpt-4o"
        self._image_models = ["gpt-image-1", "dall-e-3"]
        self._embedding_model = "text-embedding-3-small"

    # ---------- Модели ----------
    def list_models_for_menu(self) -> List[Tuple[str, bool]]:
        """
        Возвращает список моделей для меню:
        [(name, locked_flag)], где locked=True означает «модель видна, но недоступна для вызова».
        Если OpenAI не вернул расширенный список (например, нет прав), возвращаем безопасный набор.
        """
        if self._models_cache is not None:
            return self._models_cache

        try:
            data = self.client.models.list()
            names = sorted(m.id for m in data.data if isinstance(m.id, str))
            # Фильтруем chat/response модели (наивно)
            allow_keywords = ["gpt-4", "gpt-4o", "o1", "o3"]
            models = []
            for n in names:
                if any(k in n for k in allow_keywords):
                    # Пытаемся понять доступность: в SDK нет простого флага — пометим всё как доступное.
                    models.append((n, False))
            # Гарантируем несколько базовых, если по API пришло пусто
            if not models:
                models = [
                    ("gpt-4o", False),
                    ("gpt-4o-mini", False),
                    ("gpt-4.1", False),
                    ("o3-mini", True),  # возможно недоступна -> помечаем lock
                    ("o1-mini", True),
                ]
        except Exception as e:
            logger.warning("list_models_for_menu: fallback due to error: %s", e)
            models = [
                ("gpt-4o", False),
                ("gpt-4o-mini", False),
                ("gpt-4.1", False),
                ("o3-mini", True),
                ("o1-mini", True),
            ]

        self._models_cache = models
        return models

    # ---------- Чат ----------
    def _mode_preamble(self, mode: str) -> str:
        if mode == "Expert":
            return (
                "Ты — эксперт с глубоким опытом. Дай структурированный, техничный ответ, "
                "с ссылками на стандарты/источники при наличии."
            )
        if mode == "User":
            return (
                "Объясняй просто и коротко, без перегруза терминами. Дай практические шаги."
            )
        if mode == "CEO":
            return (
                "Отвечай кратко и по делу, уровень — СЕО: риски, ROI, сроки, варианты решения."
            )
        # Pro (по умолчанию)
        return "Отвечай профессионально, полно и по существу, опираясь на факты."

    def chat(
        self,
        user_text: str,
        model: Optional[str],
        temperature: float,
        mode: str,
        kb_context: Optional[str],
    ) -> str:
        """
        Унифицированный чат через Responses API (без messages).
        """
        model = model or self._default_chat_model
        system = self._mode_preamble(mode)
        if kb_context:
            system += (
                "\n\nНиже — релевантные выдержки из Базы знаний. "
                "Отвечай прежде всего на их основе; если информации недостаточно — скажи об этом и дополни общими знаниями.\n"
                f"{kb_context}"
            )

        try:
            resp = self.client.responses.create(
                model=model,
                temperature=temperature,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text},
                ],
            )
            # Извлекаем текст
            chunks = []
            for out in resp.output:
                if out.type == "message" and out.message and out.message.content:
                    for part in out.message.content:
                        if part.get("type") == "output_text":
                            chunks.append(part.get("text", ""))
            text = "".join(chunks).strip()
            return text or "Не удалось получить ответ от модели."
        except APIError as e:
            logger.exception("responses.create failed: %s", e)
            raise
        except Exception as e:
            logger.exception("chat() failed: %s", e)
            raise

    # ---------- Транскрибация ----------
    def transcribe_audio(self, bytes_data: bytes) -> str:
        """
        Whisper (или gpt-4o-mini-transcribe при наличии).
        """
        # Whisper стабильнее по доступности
        try:
            audio_file = io.BytesIO(bytes_data)
            audio_file.name = "audio.ogg"
            tr = self.client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
            return tr.text or ""
        except Exception as e:
            logger.exception("transcribe_audio failed: %s", e)
            raise

    # ---------- Изображения ----------
    def generate_image(self, prompt: str, model: Optional[str]) -> Tuple[bytes, str]:
        """
        Возвращает (image_bytes, used_prompt).
        Пробуем primary -> fallback. Поддержка b64_json и url.
        """
        models_to_try = [model] if model else []
        for m in self._image_models:
            if m not in models_to_try:
                models_to_try.append(m)

        last_err = None
        for mdl in models_to_try:
            try:
                res = self.client.images.generate(model=mdl, prompt=prompt, size="1024x1024")
                if not res or not res.data:
                    raise RuntimeError("Images API returned empty data.")
                datum = res.data[0]
                if getattr(datum, "b64_json", None):
                    return base64.b64decode(datum.b64_json), prompt
                if getattr(datum, "url", None):
                    # Скачиваем по URL
                    r = requests.get(datum.url, timeout=30)
                    r.raise_for_status()
                    return r.content, prompt
                raise RuntimeError("Images API did not return base64 or url.")
            except Exception as e:
                logger.warning("Image generate failed on %s: %s", mdl, e)
                last_err = e
                continue
        raise RuntimeError(f"Image generation failed: {last_err}")

    # ---------- Анализ вложений ----------
    def describe_file(self, bytes_data: bytes, filename: str) -> str:
        """
        Очень простое описание: тип файла, размер, базовые эвристики.
        (При необходимости можно расширить: быстрый OCR, извлечение заголовка и т.п.)
        """
        size_kb = len(bytes_data) / 1024
        ext = filename.split(".")[-1].lower() if "." in filename else "unknown"
        hint = "Похоже на документ." if ext in {"pdf", "doc", "docx", "txt"} else "Тип файла не распознан."
        return f"Имя: {filename}\nРазмер: ~{size_kb:.1f} КБ\nТип: {ext}\nКомментарий: {hint}"

    def describe_image(self, image_bytes: bytes) -> str:
        """
        Накидной вариант: можно отправить картинку в Vision-модель, но чтобы не усложнять — даем stub.
        """
        return "Изображение получено. (Анализ содержимого можно расширить через Vision-модель при необходимости.)"

    # ---------- Веб-поиск ----------
    def web_answer(self, query: str) -> Tuple[str, List[str]]:
        """
        Простой веб-поиск через DuckDuckGo HTML с follow_redirects.
        Возвращает (сводка_модели, [url, ...]).
        """
        try:
            # 1) Поиск
            q = requests.utils.quote(query)
            url = f"https://duckduckgo.com/html/?q={q}"
            html = requests.get(url, timeout=30, allow_redirects=True).text

            # 2) Извлечь ссылки (наивно)
            links = re.findall(r'href="(https?://[^"]+)"', html)
            # Отфильтруем ссылки на сам DuckDuckGo и служебные
            sources = [u for u in links if "duckduckgo.com" not in u]
            sources = list(dict.fromkeys(sources))  # уникальные, сохраняя порядок
            sources = sources[:5]

            # 3) Сформировать короткую сводку (можно упростить — без загрузки контента по каждой ссылке)
            summary = (
                f"По запросу «{query}» найдено {len(sources)} источников. "
                "Сформулирую ответ кратко на основе доступных сниппетов и общих знаний."
            )
            return summary, sources
        except Exception as e:
            logger.exception("web_answer failed: %s", e)
            raise
