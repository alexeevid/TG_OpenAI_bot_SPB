from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Sequence

from openai import PermissionDeniedError, BadRequestError, AuthenticationError

from ..clients.openai_client import OpenAIClient


class ImageService:
    """
    Единый сервис генерации изображений.

    Важно:
    - gpt-image-1 может быть виден в models.list(), но быть запрещён по org verification -> 403.
    - поэтому здесь есть runtime fallback на dall-e-3/dall-e-2.
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

    def _fallback_models(self, preferred: str) -> Sequence[str]:
        # порядок важен
        # preferred -> dall-e-3 -> dall-e-2
        base = [preferred, "dall-e-3", "dall-e-2"]
        out = []
        seen = set()
        for m in base:
            m = (m or "").strip()
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    async def generate_url(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        size: Optional[str] = None,
        dialog_settings: Optional[Dict[str, Any]] = None,
    ) -> str:
        desired_model = model or self._pick_from_dialog_settings(dialog_settings, "image_model") or self._model
        use_size = size or self._default_size

        # сначала мягко нормализуем модель по списку моделей
        safe_primary = await asyncio.to_thread(
            self._client.ensure_model_available,
            model=desired_model,
            kind="image",
            fallback=self._model,
        )

        # затем пробуем генерацию с runtime fallback (на случай 403/политик)
        last_err: Optional[Exception] = None
        for candidate in self._fallback_models(safe_primary):
            try:
                # Важно: generate_image_url должен существовать в OpenAIClient (см. патч)
                return await asyncio.to_thread(
                    self._client.generate_image_url,
                    prompt=prompt,
                    model=candidate,
                    size=use_size,
                )
            except PermissionDeniedError as e:
                # твой кейс: 403 Verify Organization для gpt-image-1
                last_err = e
                continue
            except (BadRequestError, AuthenticationError) as e:
                # неверный формат/недоступна модель/ключ и т.д. -> пробуем следующую
                last_err = e
                continue
            except Exception as e:
                last_err = e
                continue

        raise last_err or RuntimeError("Image generation failed")
