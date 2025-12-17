from __future__ import annotations

import logging
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openai import OpenAI

log = logging.getLogger(__name__)


class OpenAIClient:
    """Единый клиент OpenAI SDK (v1.x).

    Поддерживает:
    - Responses API (если доступно в установленной версии SDK)
    - fallback на Chat Completions
    - images.generate
    - audio.transcriptions
    - models.list (для динамического свитчера)
    """

    def __init__(self, api_key: Optional[str] = None):
        self.client = OpenAI(api_key=api_key)

    # -------- models --------
    def list_models(self) -> List[str]:
        try:
            out = self.client.models.list()
            ids = []
            for m in getattr(out, "data", []) or []:
                mid = getattr(m, "id", None)
                if mid:
                    ids.append(str(mid))
            return sorted(set(ids))
        except Exception as e:
            log.warning("OpenAI list_models failed: %s", e)
            return []

    # -------- text generation --------
    def generate_text(
        self,
        *,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_output_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
    ) -> str:
        # 1) Try Responses API if present in the installed SDK.
        try:
            if hasattr(self.client, "responses") and hasattr(self.client.responses, "create"):
                # Convert messages -> instructions + input (best-effort).
                system_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
                user_parts = [m.get("content", "") for m in messages if m.get("role") == "user"]
                other = [m for m in messages if m.get("role") not in ("system", "user")]

                instructions = "\n\n".join([p for p in system_parts if p]).strip() or None
                # If there are assistant turns, keep them in input as a simple transcript.
                transcript = []
                for m in messages:
                    r = m.get("role")
                    c = m.get("content", "")
                    if not c:
                        continue
                    transcript.append(f"{r}: {c}")
                input_text = "\n".join(transcript).strip() if other else ("\n\n".join(user_parts).strip())

                kwargs: Dict[str, Any] = {
                    "model": model,
                    "input": input_text,
                }
                if instructions:
                    kwargs["instructions"] = instructions
                if max_output_tokens is not None:
                    kwargs["max_output_tokens"] = int(max_output_tokens)
                # Some SDK versions support reasoning settings; pass only if provided.
                if reasoning_effort:
                    kwargs["reasoning"] = {"effort": reasoning_effort}

                resp = self.client.responses.create(**kwargs)

                # Parse output text robustly.
                if hasattr(resp, "output_text") and resp.output_text:
                    return str(resp.output_text)

                # Fallback parsing for older structures
                out = getattr(resp, "output", None)
                if isinstance(out, list):
                    for item in out:
                        content = getattr(item, "content", None)
                        if isinstance(content, list):
                            for c in content:
                                txt = getattr(c, "text", None)
                                if txt:
                                    return str(txt)
                # Last resort
                return str(resp)

        except Exception as e:
            log.info("Responses API path failed, fallback to chat.completions: %s", e)

        # 2) Chat Completions fallback.
        resp = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()

    # -------- images --------
    def generate_image_url(self, *, model: str, prompt: str, size: str = "1024x1024") -> str:
        out = self.client.images.generate(model=model, prompt=prompt, size=size)
        return out.data[0].url

    # -------- speech-to-text --------
    def transcribe_bytes(self, audio_bytes: bytes, filename: str = "audio.ogg", model: str = "whisper-1") -> str:
        bio = BytesIO(audio_bytes)
        bio.name = filename
        res = self.client.audio.transcriptions.create(model=model, file=bio)
        return (res.text or "").strip()

    def transcribe_file(self, fobj, model: str = "whisper-1") -> str:
        res = self.client.audio.transcriptions.create(model=model, file=fobj)
        return (res.text or "").strip()
