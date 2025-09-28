
from typing import List
from openai import OpenAI
import io

class OpenAIClient:
    def __init__(self, api_key: str | None):
        self.client = OpenAI(api_key=api_key) if api_key else None

    def is_enabled(self) -> bool:
        return self.client is not None

    def chat(self, messages: list[dict], model: str, temperature: float=0.7) -> str:
        if not self.client:
            return messages[-1].get("content","")
        resp = self.client.chat.completions.create(model=model, messages=messages, temperature=temperature)
        return resp.choices[0].message.content

    def embeddings(self, texts: List[str], model: str) -> list[list[float]]:
        if not self.client:
            return [[0.0]*3 for _ in texts]
        out = self.client.embeddings.create(model=model, input=texts)
        return [item.embedding for item in out.data]

    def transcribe(self, audio_bytes: bytes, model: str="whisper-1") -> str:
        if not self.client:
            return "[voice message]"
        file_like = io.BytesIO(audio_bytes); file_like.name = "audio.ogg"
        resp = self.client.audio.transcriptions.create(model=model, file=file_like)
        text = getattr(resp, "text", None) or getattr(resp, "output_text", None)
        return text or ""

    def image(self, prompt: str, model: str) -> str:
        if not self.client:
            return "https://via.placeholder.com/512?text=image+stub"
        out = self.client.images.generate(model=model, prompt=prompt)
        return out.data[0].url
