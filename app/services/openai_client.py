from __future__ import annotations

from openai import OpenAI

class OpenAIClient:
    """Thin wrapper around OpenAI Python SDK (v1.x)."""

    def __init__(self, api_key: str | None = None, default_headers: dict | None = None):
        # If api_key is None/empty, SDK will fall back to env var OPENAI_API_KEY.
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if default_headers:
            kwargs["default_headers"] = default_headers
        self.client = OpenAI(**kwargs)

    def chat_text(self, *, model: str, messages: list[dict]) -> str:
        resp = self.client.chat.completions.create(model=model, messages=messages)
        return resp.choices[0].message.content or ""

    def generate_image_url(self, *, model: str, prompt: str, size: str = "1024x1024") -> str:
        resp = self.client.images.generate(model=model, prompt=prompt, size=size)
        # OpenAI images API returns URL or b64 depending on config; assume URL default.
        return resp.data[0].url

    def transcribe(self, *, model: str, file_obj) -> str:
        resp = self.client.audio.transcriptions.create(model=model, file=file_obj)
        return resp.text
