import logging
from typing import List, Dict, Any, Optional, Tuple
from base64 import b64decode

from openai import OpenAI

logger = logging.getLogger(__name__)


class OpenAIHelper:
    """
    Обёртка над OpenAI SDK.

    - Текст: через Responses API.
    - Изображения: через Images API (generate), с управлением моделью из ENV (IMAGE_MODEL).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        image_model: Optional[str] = None,
        settings: Optional[object] = None,  # допускаем передачу Settings для удобства
    ):
        # Развязываем параметры, если передали settings
        if settings is not None:
            api_key = api_key or getattr(settings, "openai_api_key", None)
            model = model or getattr(settings, "openai_model", None)
            # image_model допускается None (тогда fallback ниже)
            if image_model is None:
                image_model = getattr(settings, "image_model", None)

        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required")

        self.client = OpenAI(api_key=api_key)
        self.model = model or "gpt-4o-mini"
        # Если есть IMAGE_MODEL — используем её, иначе будем пробовать gpt-image-1 -> dall-e-3
        self.image_model = image_model

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
        Unified chat через Responses API.
        messages: [{"role": "system"/"user"/"assistant", "content": "..."}]
        """
        try:
            resp = self.client.responses.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
            # Современное свойство SDK
            if hasattr(resp, "output_text"):
                return resp.output_text or ""
            # fallback на явный парсинг
            ch = getattr(resp, "choices", [])
            if ch:
                msg = getattr(ch[0], "message", None)
                if msg is not None:
                    # message.content может быть str или list of parts
                    content = getattr(msg, "content", "")
                    if isinstance(content, list) and content:
                        # берём текстовые части
                        parts = []
                        for p in content:
                            # на всякий случай вытаскиваем .text, .content и пр.
                            if isinstance(p, str):
                                parts.append(p)
                            else:
                                t = getattr(p, "text", None) or getattr(p, "content", None) or ""
                                if t:
                                    parts.append(t)
                        return "\n".join([p for p in parts if p]) or ""
                    if isinstance(content, str):
                        return content
            return ""
        except Exception as e:
            logger.exception("OpenAI chat failed: %s", e)
            raise

    # -------------------- Images --------------------
    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> Tuple[bytes, str, str]:
        """
        Генерирует изображение.
        Возвращает: (png_bytes, used_prompt, used_model).

        Приоритет:
        1) Если self.image_model задана (например, 'dall-e-3') — используем её.
        2) Иначе пробуем 'gpt-image-1', при ошибке прав — fallback на 'dall-e-3'.
        """
        used_prompt = (prompt or "").strip()
        # 1) Если IMAGE_MODEL задана — строго используем её
        if self.image_model:
            png = self._images_generate(self.image_model, used_prompt, size)
            return png, used_prompt, self.image_model

        # 2) Иначе пробуем gpt-image-1 -> dall-e-3
        primary = "gpt-image-1"
        try:
            png = self._images_generate(primary, used_prompt, size)
            return png, used_prompt, primary
        except Exception as e1:
            logger.warning(
                "Primary image model '%s' failed: %s. Trying 'dall-e-3' fallback...",
                primary, e1
            )
            png = self._images_generate("dall-e-3", used_prompt, size)
            return png, used_prompt, "dall-e-3"

    def _images_generate(self, model: str, prompt: str, size: str) -> bytes:
        """
        Вызов Images API с аккуратной обработкой формата ответа.
        Сначала просим b64, если вдруг придёт URL — скачаем.
        """
        res = self.client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
            response_format="b64_json",   # явное требование base64
        )
        if not getattr(res, "data", None):
            raise RuntimeError("Empty image response")

        datum = res.data[0]
        # В объектах SDK поля доступны и как атрибуты, и как dict-ключи
        b64 = getattr(datum, "b64_json", None)
        if b64 is None and isinstance(datum, dict):
            b64 = datum.get("b64_json")

        if b64:
            return b64decode(b64)

        # Если по какой-то причине пришёл URL — докачаем.
        url = getattr(datum, "url", None)
        if url is None and isinstance(datum, dict):
            url = datum.get("url")
        if url:
            # Скачиваем синхронно — снаружи этот метод вызывается через asyncio.to_thread
            import httpx
            r = httpx.get(url, timeout=30.0)
            r.raise_for_status()
            return r.content

        raise RuntimeError("Image API returned neither b64_json nor url")
