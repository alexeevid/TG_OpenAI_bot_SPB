from __future__ import annotations

import asyncio
from typing import Optional, List, Dict, Any

from app.services.openai_client import OpenAIClient


class GenService:
    """Generation service compatible with app.main wiring."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = "gpt-4.5-turbo",
        temperature: float = 0.2,
        image_model: str = "gpt-image-1",
        transcribe_model: str = "whisper-1",
        client: Optional[OpenAIClient] = None,
    ):
        self.default_model = default_model
        self.temperature = temperature
        self.image_model = image_model
        self.transcribe_model = transcribe_model
        self.client = client or OpenAIClient(api_key=api_key)

    async def chat(
        self,
        user_msg: str,
        history: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        system: str = "",
        temperature: Optional[float] = None,
    ) -> str:
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            for m in history:
                role = m.get("role")
                content = m.get("content")
                if role and content is not None:
                    messages.append({"role": str(role), "content": str(content)})
        messages.append({"role": "user", "content": user_msg})
        use_model = model or self.default_model
        use_temp = self.temperature if temperature is None else temperature
        return await asyncio.to_thread(self.client.chat_text, model=use_model, messages=messages, temperature=use_temp)

    async def image(self, prompt: str, model: Optional[str] = None, size: str = "1024x1024") -> str:
        use_model = model or self.image_model
        return await asyncio.to_thread(self.client.generate_image_url, model=use_model, prompt=prompt, size=size)

    async def transcribe_file(self, file_obj, model: Optional[str] = None) -> str:
        use_model = model or self.transcribe_model
        return await asyncio.to_thread(self.client.transcribe, model=use_model, file_obj=file_obj)
