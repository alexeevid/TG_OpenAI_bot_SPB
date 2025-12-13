from __future__ import annotations

from typing import Optional, List, Dict, Any

from app.services.openai_client import OpenAIClient


class GenService:
    """Generation service compatible with existing app.main wiring.

    app.main expects:
        GenService(api_key=cfg.openai_api_key, default_model=cfg.text_model)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = "gpt-4.5-turbo",
        image_model: str = "gpt-image-1",
        transcribe_model: str = "whisper-1",
    ):
        self.default_model = default_model
        self.image_model = image_model
        self.transcribe_model = transcribe_model
        self.client = OpenAIClient(api_key=api_key)

    def chat(
        self,
        prompt: str,
        history: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        system: str = "",
    ) -> str:
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            # history is expected to be a list of {"role": ..., "content": ...}
            for m in history:
                role = m.get("role")
                content = m.get("content")
                if role and content is not None:
                    messages.append({"role": str(role), "content": str(content)})
        messages.append({"role": "user", "content": prompt})
        use_model = model or self.default_model
        return self.client.chat_text(model=use_model, messages=messages)

    def image(self, prompt: str, model: Optional[str] = None, size: str = "1024x1024") -> str:
        use_model = model or self.image_model
        return self.client.generate_image_url(model=use_model, prompt=prompt, size=size)

    def transcribe_file(self, file_obj, model: Optional[str] = None) -> str:
        use_model = model or self.transcribe_model
        return self.client.transcribe(model=use_model, file_obj=file_obj)
