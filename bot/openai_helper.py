import logging
from typing import List, Dict, Any, Optional, Tuple
from base64 import b64decode
import httpx

from openai import OpenAI

logger = logging.getLogger(__name__)


class OpenAIHelper:
    """
    Обёртка над OpenAI SDK.

    - Текст: прежде всего через Chat Completions API; Responses — как fallback.
    - Изображения: Images API (generate), модель берём из ENV (IMAGE_MODEL).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        image_model: Optional[str] = None,
        settings: Optional[object] = None,
    ):
        if settings is not None:
            api_key = api_key or getattr(settings, "openai_api_key", None)
            model = model or getattr(settings, "openai_model", None)
            if image_model is None:
                image_model = getattr(settings, "image_model", None)

        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required")

        self.client = OpenAI(api_key=api_key)
        self.model = model or "gpt-4o-mini"
        self.image_model = image_model  # может быть None → fallback в generate_image

    # -------------------- Text --------------------
    def set_model(self, name: str) -> None:
        self.model = name

    def list_models(self) -> List[str]:
        try:
            res = self.client.models.list()
            items = [m.id for m in getattr(res, "data", [])]
            return sorted(items)
        except Exception as e:
            logger.exception("Failed to list models: %s", e)
            return [self.model]

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float = 0.3,
        max_output_tokens: int = 2048,
    ) -> str:
        """
        Основной путь — Chat Completions (messages). Если не выйдет — пробуем Responses(input).
        """
        # 1) Chat Completions
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_output_tokens,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e1:
            logger.warning("chat.completions failed: %s; trying responses API…", e1)

        # 2) Responses API (строим input из messages)
        try:
            sys_parts, usr_parts = [], []
            for m in messages:
                role = m.get("role")
                c = m.get("content", "")
                if role == "system":
                    sys_parts.append(c)
                elif role == "user":
                    usr_parts.append(c)
                elif role == "assistant":
                    # чуть контекста тоже добавим
                    usr_parts.append(f"[ASSISTANT]\n{c}")

            sys_hint = "\n".join(sys_parts).strip()
            usr = "\n\n".join(usr_parts).strip()
            input_text = f"[SYSTEM]\n{sys_hint}\n\n{usr}" if sys_hint else usr

            resp2 = self.client.responses.create(
                model=self.model,
                input=input_text,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
            if hasattr(resp2, "output_text"):
                return resp2.output_text or ""
            ch = getattr(resp2, "choices", [])
            if ch:
                msg = getattr(ch[0], "message", None)
                if msg and isinstance(msg, dict):
                    return (msg.get("content") or "").strip()
            return ""
        except Exception as e2:
            logger.exception("OpenAI chat failed (both APIs): %s", e2)
            raise

    # -------------------- Images --------------------
    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> Tuple[bytes, str, str]:
        """
        Возвращает: (png_bytes, used_prompt, used_model).

        Приоритет:
        1) Если self.image_model задана (например, 'dall-e-3') — используем её.
        2) Иначе пробуем 'gpt-image-1', при ошибке прав — fallback 'dall-e-3'.
        """
        used_prompt = (prompt or "").strip()

        if self.image_model:
            png = self._images_generate(self.image_model, used_prompt, size)
            return png, used_prompt, self.image_model

        primary = "gpt-image-1"
        try:
            png = self._images_generate(primary, used_prompt, size)
            return png, used_prompt, primary
        except Exception as e1:
            logger.warning("Primary image model '%s' failed: %s. Trying 'dall-e-3'…", primary, e1)
            png = self._images_generate("dall-e-3", used_prompt, size)
            return png, used_prompt, "dall-e-3"

    def _images_generate(self, model: str, prompt: str, size: str) -> bytes:
        """
        Явно просим base64; если придёт URL — скачаем.
        """
        res = self.client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
            response_format="b64_json",
        )
        if not getattr(res, "data", None):
            raise RuntimeError("Empty image response")

        datum = res.data[0]
        b64 = getattr(datum, "b64_json", None)
        if b64 is None and isinstance(datum, dict):
            b64 = datum.get("b64_json")

        if b64:
            return b64decode(b64)

        url = getattr(datum, "url", None)
        if url is None and isinstance(datum, dict):
            url = datum.get("url")
        if url:
            r = httpx.get(url, timeout=30.0)
            r.raise_for_status()
            return r.content

        raise RuntimeError("Image API returned neither b64_json nor url")
