from __future__ import annotations

import logging
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Tuple

import openai
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
        """Generate assistant text.

        Notes:
        - For GPT‑5.* we prefer the Responses API (recommended by OpenAI).
        - We map any `system` messages into the `instructions` field for Responses.
        - If the selected model is not available on the key/account, we surface the API error.
        """

        # Split system instructions from conversational turns.
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
                # Responses API supports role/content as strings (user/assistant).
                turns.append({"role": role or "user", "content": content_s})

        instructions = "

".join(sys_parts).strip() or None

        def _extract_output_text(resp: Any) -> str:
            # SDKs expose output_text in newer versions.
            out_txt = getattr(resp, "output_text", None)
            if out_txt:
                return str(out_txt).strip()

            # Fallback: walk response.output[*].content[*].text
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

        # Prefer Responses API if available (and especially for GPT‑5.*).
        has_responses = hasattr(self.client, "responses") and hasattr(self.client.responses, "create")
        prefer_responses = model.startswith("gpt-5") or model.startswith("o")

        if has_responses and (prefer_responses or True):
            # We do a small retry loop to gracefully handle SDK/parameter mismatches.
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
                # Retry with fewer optional args (some models/SDK versions reject certain params).
                {k: v for k, v in base_kwargs.items() if k not in {"reasoning", "temperature"}},
                {k: v for k, v in base_kwargs.items() if k not in {"reasoning", "temperature", "max_output_tokens"}},
            ]

            last_err: Exception | None = None
            for kw in attempts:
                try:
                    resp = self.client.responses.create(**kw)
                    return _extract_output_text(resp)
                except TypeError as e:
                    # Older SDK signature mismatch.
                    last_err = e
                    continue
                except Exception as e:
                    # APIStatusError / BadRequestError etc.
                    last_err = e
                    continue

            # If GPT‑5.* was selected, do NOT silently fall back to Chat Completions;
            # surface the failure so upper layers can decide what to do.
            if prefer_responses and last_err:
                raise last_err

            # Otherwise we can try Chat Completions as a fallback.
            log.warning("Responses API failed, falling back to chat.completions: %s", last_err)

        # Chat Completions fallback (works for gpt‑4o and earlier).
        resp = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=float(temperature),
        )
        return (resp.choices[0].message.content or "").strip()

    # -------- images --------
 --------
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
