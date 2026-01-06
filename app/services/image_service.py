# app/services/image_service.py
from __future__ import annotations

import asyncio
from typing import Optional

from ..clients.openai_client import OpenAIClient


class ImageService:
    """
    Единый сервис генерации изображений.
    Источник истины: OpenAIClient.generate_image_url()
    """

    def __init__(self, api_key: str, image_model: str = "gpt-image-1", default_size: str = "1024x1024"):
        self._client = OpenAIClient(api_key=api_key)
        self._model = image_model or "gpt-image-1"
        self._default_size = default_size or "1024x1024"

    async def generate_url(self, prompt: str, *, model: Optional[str] = None, size: Optional[str] = None) -> str:
        use_model = model or self._model
        use_size = size or self._default_size
        return await asyncio.to_thread(
            self._client.generate_image_url,
            prompt=prompt,
            model=use_model,
            size=use_size,
        )
