# app/services/image_service.py
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

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

    def _pick_from_dialog_settings(self, dialog_settings: Optional[Dict[str, Any]], key: str) -> Optional[str]:
        if dialog_settings and isinstance(dialog_settings, dict):
            v = dialog_settings.get(key)
            if v:
                return str(v)
        return None

    async def generate_url(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        size: Optional[str] = None,
        dialog_settings: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        model:
          - если передан явно -> используем его
          - иначе берём dialog_settings["image_model"]
          - иначе self._model
        """
        desired_model = model or self._pick_from_dialog_settings(dialog_settings, "image_model") or self._model
        use_size = size or self._default_size

        # Мягкая валидация доступности модели (по API key)
        safe_model = await asyncio.to_thread(
            self._client.ensure_model_available,
            model=desired_model,
            kind="image",
            fallback=self._model,
        )

        return await asyncio.to_thread(
            self._client.generate_image_url,
            prompt=prompt,
            model=safe_model,
            size=use_size,
        )
