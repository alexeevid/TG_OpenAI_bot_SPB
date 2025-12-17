from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..clients.openai_client import OpenAIClient

log = logging.getLogger(__name__)


class GenService:
    """Сервис генерации текста/изображений/транскрибации.

    Принцип:
    - синхронные вызовы OpenAI SDK выполняются в threadpool (asyncio.to_thread)
    - модель выбирается из настроек диалога (settings JSON)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = "gpt-5.2",
        temperature: float = 0.2,
        max_output_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        image_model: str = "gpt-image-1",
        transcribe_model: str = "whisper-1",
    ):
        self.client = OpenAIClient(api_key=api_key)
        self.default_model = default_model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.image_model = image_model
        self.transcribe_model = transcribe_model

    async def list_models(self) -> List[str]:
        return await asyncio.to_thread(self.client.list_models)

    def _rank_models(self, models: List[str]) -> List[str]:
        # Prefer "latest & strong" first, then others.
        preferred = [
            "gpt-5.2-pro",
            "gpt-5.2",
            "gpt-5.1",
            "gpt-5",
            "gpt-4.1",
            "gpt-4o",
            "gpt-4o-mini",
        ]
        s = set(models)
        ordered = [m for m in preferred if m in s]
        rest = sorted([m for m in models if m not in set(ordered)])
        return ordered + rest

    async def selectable_models(self, limit: int = 12) -> List[str]:
        models = await self.list_models()
        # Filter typical chat-capable families
        filtered = [m for m in models if m.startswith(("gpt-", "o"))]
        ranked = self._rank_models(filtered)
        if ranked:
            return ranked[:limit]
        # Safe fallback if listing fails
        return ["gpt-5.2", "gpt-5.2-pro", "gpt-4o", "gpt-4o-mini"]

    async def chat(
        self,
        user_msg: str,
        history: Optional[List[Dict[str, str]]] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> str:
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history:
            for m in history:
                r = m.get("role")
                c = m.get("content")
                if r and c is not None:
                    messages.append({"role": str(r), "content": str(c)})
        messages.append({"role": "user", "content": user_msg})

        use_model = model or self.default_model
        use_temp = self.temperature if temperature is None else float(temperature)

        return await asyncio.to_thread(
            self.client.generate_text,
            model=use_model,
            messages=messages,
            temperature=use_temp,
            max_output_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort,
        )

    async def image(self, prompt: str, model: Optional[str] = None, size: str = "1024x1024") -> str:
        use_model = model or self.image_model
        return await asyncio.to_thread(self.client.generate_image_url, model=use_model, prompt=prompt, size=size)

    async def transcribe_file(self, file_obj, model: Optional[str] = None) -> str:
        use_model = model or self.transcribe_model
        return await asyncio.to_thread(self.client.transcribe_file, file_obj, use_model)
