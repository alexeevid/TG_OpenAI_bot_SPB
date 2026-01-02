from __future__ import annotations

import base64
import logging
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence

from openai import OpenAI

log = logging.getLogger(__name__)


class OpenAIClient:
    """Единый клиент OpenAI SDK (v1.x) для:
    - генерации текста (Responses API с fallback на Chat Completions)
    - эмбеддингов
    - генерации изображений
    - распознавания речи
    - (опционально) извлечения текста из изображений через Responses API
    """

    def __init__(self, api_key: Optional[str] = None):
        self.client = OpenAI(api_key=api_key)

    def is_enabled(self) -> bool:
        return self.client is not None

    def list_models(self) -> List[str]:
        try:
            out = self.client.models.list()
            ids: List[str] = []
            for m in getattr(out, "data", []) or []:
                mid = getattr(m, "id", None)
                if mid:
                    ids.append(str(mid))
            return sorted(set(ids))
        except Exception as e:
            log.warning("OpenAI list_models failed: %s", e)
            return []

    def generate_text(
        self,
        *,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_output_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
    ) -> str:
        sys_parts: List[str] = []
        turns: List[Dict[str, str]] = []
        for m in (messages or []):
            role = (m.get("role") or "").strip()
            content = m.get("content")
            if content is None:
                continue
            content_s = str(content)
            if role == "system":
                if content_s.strip():
                    sys_parts.append(content_s.strip())
            else:
                turns.append({"role": role or "user", "content": content_s})

        instructions = "\n\n".join(sys_parts).strip() or None

        def _extract_output_text(resp: Any) -> str:
            out_txt = getattr(resp, "output_text", None)
            if out_txt:
                return str(out_txt).strip()
            out = getattr(resp, "output", None)
            if isinstance(out, list):
                for item in out:
                    content = getattr(item, "content", None)
                    if isinstance(content, list):
                        for c in content:
                            t = getattr(c, "text", None)
                            if t:
                                return str(t).strip()
            return str(resp).strip()

        has_responses = hasattr(self.client, "responses") and hasattr(self.client.responses, "create")
        prefer_responses = model.startswith("gpt-5") or model.startswith("o") or model.startswith("gpt-4o")

        if has_responses and prefer_responses:
            base_kwargs: Dict[str, Any] = {"model": model, "input": turns if turns else ""}
            if instructions:
                base_kwargs["instructions"] = instructions
            if temperature is not None:
                base_kwargs["temperature"] = float(temperature)
            if max_output_tokens:
                base_kwargs["max_output_tokens"] = int(max_output_tokens)
            if reasoning_effort:
                base_kwargs["reasoning"] = {"effort": reasoning_effort}

            attempts = [
                base_kwargs,
                {k: v for k, v in base_kwargs.items() if k not in {"reasoning", "temperature"}},
                {k: v for k, v in base_kwargs.items() if k not in {"reasoning", "temperature", "max_output_tokens"}},
            ]
            last_err: Exception | None = None
            for kw in attempts:
                try:
                    resp = self.client.responses.create(**kw)
                    return _extract_output_text(resp)
                except Exception as e:
                    last_err = e
                    continue
            if last_err:
                raise last_err

        resp = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=float(temperature),
        )
        return (resp.choices[0].message.content or "").strip()

    def embeddings(self, texts: Sequence[str], model: str) -> List[List[float]]:
        resp = self.client.embeddings.create(model=model, input=list(texts))
        out: List[List[float]] = []
        for item in resp.data:
            out.append(list(item.embedding))
        return out

    def embed(self, texts: Sequence[str], model: str) -> List[List[float]]:
        return self.embeddings(texts, model=model)

    def generate_image_url(self, *, model: str, prompt: str, size: str = "1024x1024") -> str:
        out = self.client.images.generate(model=model, prompt=prompt, size=size)
        return out.data[0].url

    def transcribe_file(self, fobj, model: str = "whisper-1") -> str:
        """Распознавание речи. fobj — бинарный file-like (open(...,'rb'))."""
        res = self.client.audio.transcriptions.create(model=model, file=fobj)
        return (res.text or "").strip()

    def transcribe_bytes(self, audio_bytes: bytes, filename: str = "audio.ogg", model: str = "whisper-1") -> str:
        bio = BytesIO(audio_bytes)
        bio.name = filename
        return self.transcribe_file(bio, model=model)

    def vision_extract_text(self, image_bytes: bytes, *, model: str = "gpt-4o-mini") -> str:
        if not (hasattr(self.client, "responses") and hasattr(self.client.responses, "create")):
            raise RuntimeError("Responses API is not available in installed OpenAI SDK")

        b64 = base64.b64encode(image_bytes).decode("ascii")
        resp = self.client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Извлеки текст с изображения. Если текста нет, кратко опиши содержание."},
                        {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"},
                    ],
                }
            ],
        )
        return (getattr(resp, "output_text", None) or "").strip()
