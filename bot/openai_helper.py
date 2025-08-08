from __future__ import annotations
from typing import List, Optional, Dict, Any
from openai import AsyncOpenAI
from bot.settings import load_settings

_settings = load_settings()
_client = AsyncOpenAI(api_key=_settings.openai_api_key)

async def chat(messages: List[Dict[str, str]], model: Optional[str] = None, max_tokens: int = 800) -> str:
    model = model or _settings.openai_model
    resp = await _client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.2,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""

async def embed(texts: List[str], model: Optional[str] = None) -> List[List[float]]:
    model = model or _settings.embedding_model
    resp = await _client.embeddings.create(model=model, input=texts)
    return [d.embedding for d in resp.data]

async def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        tr = await _client.audio.transcriptions.create(
            model="whisper-1",
            file=f
        )
    return tr.text or ""

async def generate_image(prompt: str) -> bytes:
    img = await _client.images.generate(model=_settings.image_model, prompt=prompt, size="1024x1024")
    url = img.data[0].url
    import requests
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content
