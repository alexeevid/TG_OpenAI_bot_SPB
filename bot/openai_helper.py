import logging
from typing import List, Optional, Tuple, Dict, Any
from base64 import b64decode, b64encode

from openai import OpenAI
from openai import APIError, APIConnectionError, BadRequestError, PermissionDeniedError

logger = logging.getLogger(__name__)


class OpenAIHelper:
    """
    Обёртка над OpenAI SDK.
    Поддерживает:
      - chat()                — диалог (Chat Completions)
      - list_models()         — список моделей
      - generate_image()      — генерация изображений (gpt-image-1 / dall-e-3)
      - transcribe()          — STT (Speech-to-Text)
      - analyze_image()       — Vision-анализ фото/изображений
      - answer_with_web()     — Responses API + web_search
    """

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        image_primary: Optional[str] = None,
        image_fallback: Optional[str] = None,
        stt_model: Optional[str] = None,
        **kwargs,
    ):
        """
        Параметры можно передать из Settings:
          - model: основной текстовый (например, "gpt-4o")
          - image_primary: модель генерации изображений (например, "gpt-image-1")
          - image_fallback: запасная ("dall-e-3")
          - stt_model: модель распознавания (например, "gpt-4o-mini-transcribe")
        """
        self.client = OpenAI(api_key=api_key)
        self.model = model or kwargs.get("default_model") or kwargs.get("openai_model") or "gpt-4o"
        self.image_primary = image_primary or kwargs.get("image_model") or "gpt-image-1"
        self.image_fallback = image_fallback or "dall-e-3"
        self.stt_model = stt_model or "gpt-4o-mini-transcribe"

    # -------- Models --------
    def list_models(self) -> List[str]:
        try:
            data = self.client.models.list()
            ids = [m.id for m in getattr(data, "data", [])]
            # приоритизируем «часто используемые»
            prefer = ["gpt-4o", "gpt-4.1", "gpt-4", "gpt-3.5", "o4", "o3"]
            ids_sorted = sorted(
                ids,
                key=lambda i: (0 if any(k in i for k in prefer) else 1, i),
            )
            return ids_sorted
        except Exception as e:
            logger.exception("list_models failed: %s", e)
            return [self.model]

    # -------- Chat --------
    def set_model(self, m: str):
        self.model = m

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float = 0.2,
        max_output_tokens: int = 4096,
        model: Optional[str] = None,
    ) -> str:
        """
        Простая обёртка над Chat Completions. Возвращает text content первого выбора.
        """
        use_model = model or self.model
        try:
            resp = self.client.chat.completions.create(
                model=use_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_output_tokens,
            )
            choice = resp.choices[0]
            return choice.message.content or ""
        except Exception as e:
            logger.exception("chat() failed: %s", e)
            raise

    # -------- Images --------
    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> Tuple[bytes, str, str]:
        """
        Возвращает (png_bytes, used_prompt, used_model).
        Пытается сначала image_primary, затем fallback.
        """
        def _call(model_name: str) -> Tuple[bytes, str, str]:
            res = self.client.images.generate(
                model=model_name,
                prompt=prompt,
                size=size,
                response_format="b64_json",
            )
            data = getattr(res, "data", None) or []
            b64 = data[0].b64_json if data else None
            if not b64:
                raise ValueError("Empty image data returned")
            return b64decode(b64), prompt, model_name

        try:
            return _call(self.image_primary)
        except PermissionDeniedError as e:
            logger.warning(
                "Primary image model '%s' failed: %s. Trying '%s' fallback...",
                self.image_primary, e, self.image_fallback
            )
            return _call(self.image_fallback)
        except Exception as e:
            logger.exception("Image generation failed: %s", e)
            raise

    # -------- Speech-to-Text --------
    def transcribe(self, file_path: str) -> str:
        """
        Распознаёт речь из аудиофайла (mp3/m4a/wav/webm/ogg*) и возвращает текст.
        """
        try:
            with open(file_path, "rb") as f:
                tr = self.client.audio.transcriptions.create(
                    model=self.stt_model,
                    file=f,
                )
            text = getattr(tr, "text", None)
            if not text:
                raise ValueError("Empty transcription result")
            return text
        except Exception as e:
            logger.exception("STT failed: %s", e)
            raise

    # -------- Vision: анализ изображения --------
    def analyze_image(
        self,
        file_path: str,
        *,
        prompt: Optional[str] = None,
        model: Optional[str] = None,
        detail: Optional[str] = None,          # 'low' | 'high' (если указано)
        max_tokens: int = 600,
    ) -> str:
        """
        Анализ изображения через Responses API.
        Возвращает текстовое описание/резюме.
        """
        use_model = model or self.model or "gpt-4o"
        try:
            with open(file_path, "rb") as f:
                b64 = b64encode(f.read()).decode("ascii")

            img_obj: Dict[str, Any] = {"type": "input_image", "image_data": b64}
            if detail:
                img_obj["detail"] = detail  # поддерживается не всеми моделями; если нет — тихо игнорится

            content = [
                img_obj,
                {"type": "text", "text": prompt or "Опиши изображение, выдели ключевые объекты, текст и возможные риски/аномалии."},
            ]

            resp = self.client.responses.create(
                model=use_model,
                input=[{"role": "user", "content": content}],
                max_output_tokens=max_tokens,
            )

            # Соберём текст из output
            chunks: List[str] = []
            for out in getattr(resp, "output", []) or []:
                if getattr(out, "type", None) == "message":
                    for c in getattr(out, "content", []) or []:
                        if getattr(c, "type", None) == "output_text" and getattr(c, "text", None):
                            chunks.append(c.text)
            return "\n".join(chunks).strip() or "Описание недоступно."
        except Exception as e:
            logger.exception("analyze_image failed: %s", e)
            return f"Не удалось проанализировать изображение: {e}"

    # -------- Responses + Web Search --------
    def answer_with_web(self, prompt: str, *, model: Optional[str] = None) -> Tuple[str, List[Dict[str, str]]]:
        """
        Выполняет запрос с включенным web_search tool и возвращает (text, citations[]).
        citations: список словарей {title, url}
        """
        use_model = model or self.model or "gpt-4o"
        try:
            resp = self.client.responses.create(
                model=use_model,
                input=prompt,
                tools=[{"type": "web_search"}],
            )

            text_chunks: List[str] = []
            citations: List[Dict[str, str]] = []

            # Структура output: список шагов (сообщения, tool_calls и т.д.)
            for out in getattr(resp, "output", []) or []:
                if getattr(out, "type", None) == "message":
                    for c in getattr(out, "content", []) or []:
                        if getattr(c, "type", None) == "output_text" and getattr(c, "text", None):
                            text_chunks.append(c.text)
                        # аннотации со ссылками (если есть)
                        for ann in (getattr(c, "annotations", []) or []):
                            if getattr(ann, "type", None) == "url_citation":
                                title = getattr(ann, "title", None)
                                url = getattr(ann, "url", None)
                                if url:
                                    citations.append({"title": title or url, "url": url})

            text = "\n".join(text_chunks).strip() or "Ничего не найдено."
            # Убираем дубликаты ссылок
            seen = set()
            uniq = []
            for it in citations:
                if it["url"] not in seen:
                    uniq.append(it)
                    seen.add(it["url"])
            return text, uniq

        except Exception as e:
            logger.exception("Web search failed: %s", e)
            return f"Ошибка web‑поиска: {e}", []
