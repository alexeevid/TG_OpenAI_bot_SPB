# bot/openai_helper.py
from __future__ import annotations

import logging
from base64 import b64decode
from typing import Dict, List, Optional, Tuple

from openai import OpenAI
from openai.types import ImagesResponse

logger = logging.getLogger(__name__)


def _extract_text_from_responses(resp) -> str:
    """
    Универсальный парсер текста из OpenAI Responses API (v1).
    Собираем все content.output_text из message-чанков.
    """
    chunks: List[str] = []

    # Новые клиенты возвращают объект со списком output-элементов
    for out in getattr(resp, "output", []) or []:
        if getattr(out, "type", None) == "message":
            for c in getattr(out, "content", []) or []:
                if getattr(c, "type", None) == "output_text" and getattr(c, "text", None):
                    chunks.append(c.text)

    # На всякий случай — некоторые клиенты кладут первичный текст прямо в resp.output_text
    if not chunks and getattr(resp, "output_text", None):
        chunks.append(resp.output_text)

    text = "\n".join(chunks).strip()
    return text


class OpenAIHelper:
    """
    Обёртка над OpenAI SDK (client v1.*) с:
      - chat()   — текстовые ответы через Responses API,
      - transcribe() — распознавание аудио Whisper,
      - generate_image() — генерация изображений (с fallback),
      - answer_with_web() — вызов web_search tool + возврат ссылок.
    """

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        image_model: Optional[str] = None,
        temperature: float = 0.2,
        enable_image_generation: bool = True,
    ):
        self.client = OpenAI(api_key=api_key)
        self.model = model or "gpt-4o"
        self.image_model = image_model or "dall-e-3"
        self.temperature = temperature
        self.enable_image_generation = enable_image_generation

    # ---------- Text / Chat ----------

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
    ) -> str:
        """
        messages: [{"role": "system"|"user"|"assistant", "content": "..."}]
        Используем Responses API с одним input (склеиваем system + user/assistant).
        """
        use_model = model or self.model
        temp = self.temperature if temperature is None else temperature

        sys_buf: List[str] = []
        conv_buf: List[str] = []

        for m in messages:
            role = (m.get("role") or "").lower()
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if role == "system":
                sys_buf.append(content)
            elif role == "user":
                conv_buf.append(f"[USER]\n{content}")
            elif role == "assistant":
                conv_buf.append(f"[ASSISTANT]\n{content}")

        system_block = ""
        if sys_buf:
            # ВАЖНО: не использовать f-выражения с обратными слешами внутри {} — были SyntaxError
            system_block = "[SYSTEM]\n" + "\n\n".join(sys_buf) + "\n\n"

        prompt = system_block + "\n\n".join(conv_buf).strip()

        resp = self.client.responses.create(
            model=use_model,
            input=prompt,
            temperature=temp,
            max_output_tokens=max_output_tokens,
        )
        return _extract_text_from_responses(resp)

    # ---------- Audio ----------

    def transcribe(self, audio_path: str) -> str:
        """
        Распознавание речи (Whisper). Возвращает текст.
        """
        with open(audio_path, "rb") as f:
            tr = self.client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                # язык не фиксируем — пусть автоопределяет; можно добавить: language="ru"
            )
        return (getattr(tr, "text", None) or "").strip()

    # ---------- Images ----------

    def generate_image(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        size: str = "1024x1024",
        fallback_to_dalle3: bool = True,
    ) -> bytes:
        """
        Генерация изображения. Возвращает бинарные PNG-данные.
        Если выбран 'gpt-image-1' и нет верификации — откатываемся на 'dall-e-3' (если разрешено).
        """
        if not self.enable_image_generation:
            raise RuntimeError("Image generation is disabled by configuration.")

        primary = model or self.image_model or "dall-e-3"

        def _call(m: str) -> bytes:
            res: ImagesResponse = self.client.images.generate(
                model=m,
                prompt=prompt,
                size=size,
            )
            data = (res.data or [])
            if not data:
                raise RuntimeError("Images API returned empty data.")
            b64 = getattr(data[0], "b64_json", None)
            if not b64:
                raise RuntimeError("Images API did not return base64 image.")
            return b64decode(b64)

        try:
            return _call(primary)
        except Exception as e:
            logger.warning("Primary image model '%s' failed: %s", primary, e)
            # Специальный кейс: частая 403 при gpt-image-1 (нужна верификация организации)
            if fallback_to_dalle3 and primary != "dall-e-3":
                try:
                    return _call("dall-e-3")
                except Exception as e2:
                    logger.error("Image generation failed even with fallback: %s", e2)
                    raise
            raise

    # ---------- Web ----------

    def answer_with_web(self, prompt: str, *, model: Optional[str] = None) -> Tuple[str, List[Dict[str, str]]]:
        """
        Выполняет запрос с включенным web_search tool.
        Возвращает: (текст ответа, список цитат [{title,url}, ...]).
        """
        use_model = model or self.model or "gpt-4o"
        try:
            resp = self.client.responses.create(
                model=use_model,
                input=prompt,
                tools=[{"type": "web_search"}],
            )

            text = _extract_text_from_responses(resp)
            citations: List[Dict[str, str]] = []

            # 1) Пытаемся достать url_citation из annotations
            for out in getattr(resp, "output", []) or []:
                if getattr(out, "type", None) == "message":
                    for c in getattr(out, "content", []) or []:
                        for ann in (getattr(c, "annotations", []) or []):
                            if getattr(ann, "type", None) == "url_citation":
                                url = getattr(ann, "url", None)
                                title = getattr(ann, "title", None)
                                if url:
                                    citations.append({"title": title or url, "url": url})

            # 2) Если инструмент вернул список результатов — тоже соберём
            for out in getattr(resp, "output", []) or []:
                if getattr(out, "type", None) == "tool_result":
                    name = (getattr(out, "tool_name", None) or getattr(out, "name", None) or "").lower()
                    if name == "web_search":
                        data = getattr(out, "content", None) or getattr(out, "result", None)
                        if isinstance(data, list):
                            for item in data:
                                if isinstance(item, dict) and item.get("url"):
                                    citations.append({"title": item.get("title") or item["url"], "url": item["url"]})

            # Уникализируем по URL
            seen = set()
            uniq: List[Dict[str, str]] = []
            for it in citations:
                if it["url"] not in seen:
                    uniq.append(it)
                    seen.add(it["url"])

            return (text or "Ничего не найдено.").strip(), uniq

        except Exception as e:
            logger.exception("Web search failed: %s", e)
            return f"Ошибка web‑поиска: {e}", []

    # ---------- Models ----------

    def list_models(
        self,
        *,
        whitelist: Optional[List[str]] = None,
        denylist: Optional[List[str]] = None,
    ) -> List[str]:
        """
        Возвращает список доступных моделей (с учётом опциональных белого/чёрного списков).
        """
        try:
            mlist = self.client.models.list()
            ids = [m.id for m in getattr(mlist, "data", [])]
            if whitelist:
                ids = [m for m in ids if m in set(whitelist)]
            if denylist:
                d = set(denylist)
                ids = [m for m in ids if m not in d]
            # упорядочим: «умные» модели наверх
            prefer = ["o4", "o4-mini", "gpt-4.1", "gpt-4o", "gpt-4"]
            ids = sorted(ids, key=lambda x: (0 if any(x.startswith(p) for p in prefer) else 1, x))
            return ids
        except Exception as e:
            logger.exception("Failed to list models: %s", e)
            return []
