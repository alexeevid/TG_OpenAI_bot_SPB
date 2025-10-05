
from typing import List
import logging
from openai import OpenAI
import io
log = logging.getLogger(__name__)

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
    def transcribe_bytes(self, raw: bytes, filename: str = "audio.ogg") -> str:
        """
        Надёжная передача Whisper'у: даём file-like объект на основе BytesIO.
        """
        bio = io.BytesIO(raw)
        # openai sdk использует name для определения расширения; задаём имя
        bio.name = filename
        res = self._client.audio.transcriptions.create(
            model="whisper-1",
            file=bio,
        )
        text = (res.text or "").strip()
        return text

    def transcribe_file(self, fobj) -> str:
        """
        Альтернатива: принимаем открытый файл (rb).
        """
        res = self._client.audio.transcriptions.create(
            model="whisper-1",
            file=fobj,
        )
        text = (res.text or "").strip()
        return text

    def transcribe_path(self, path: str) -> str:
        """
        На крайний случай: открываем путь сами и отправляем file-like.
        (Не передаём путь строкой напрямую, чтобы избежать 'bytes-like required')
        """
        with open(path, "rb") as f:
            res = self._client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
            )
        text = (res.text or "").strip()
        return text
