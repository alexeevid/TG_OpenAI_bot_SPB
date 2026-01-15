# app/services/image_service.py
from __future__ import annotations

import asyncio
from typing import Optional

from ..clients.openai_client import OpenAIClient


class ImageService:
    """
    Единый сервис генерации изображений.

    ВАЖНО:
    - В OpenAIClient есть generate_image(prompt, model) (без size).
    - Здесь мы поддерживаем size, используя нативный client.images.generate(..., size=...).
    """

    def __init__(self, api_key: str, image_model: str = "gpt-image-1", default_size: str = "1024x1024"):
        self._client = OpenAIClient(api_key=api_key)
        self._model = image_model
        self._default_size = default_size

    async def generate_url(self, prompt: str, *, model: Optional[str] = None, size: Optional[str] = None) -> str:
        desired_model = (model or self._model).strip()
        use_size = (size or self._default_size).strip()

        # Мягкая валидация доступности модели (по API key)
        safe_model = await asyncio.to_thread(
            self._client.ensure_model_available,
            model=desired_model,
            kind="image",
            fallback=self._model,
        )

        def _do_generate() -> str:
            r = self._client.client.images.generate(model=safe_model, prompt=prompt, size=use_size)
            data = getattr(r, "data", None) or []
            if not data:
                raise RuntimeError("Empty image response")
            first = data[0]
            url = getattr(first, "url", None)
            if not url:
                raise RuntimeError("No image URL in response")
            return str(url)

        return await asyncio.to_thread(_do_generate)
