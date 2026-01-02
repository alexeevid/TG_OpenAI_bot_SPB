from __future__ import annotations

import logging
import time
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Tuple

import openai
from openai import OpenAI

log = logging.getLogger(__name__)


class OpenAIClient:
    """
    OpenAI API client wrapper.

    In this project we use:
    - text generation (Responses API when available, fallback to Chat Completions)
    - image generation
    - audio transcription
    - embeddings (for KB/RAG)
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
            log.warning("Failed to list models: %s", e)
            return []

    # -------- embeddings (KB/RAG) --------
    def embeddings(self, texts: Sequence[str], model: str) -> List[List[float]]:
        """
        Return embeddings for each text.
        """
        if not texts:
            return []

        # Basic retries for transient errors / rate limits.
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                resp = self.client.embeddings.create(
                    model=model,
                    input=list(texts),
                )
                return [d.embedding for d in resp.data]
            except Exception as e:
                last_err = e
                # simple backoff
                time.sleep(0.5 * attempt)

        raise last_err or RuntimeError("embeddings() failed")

    def embed(self, texts: Sequence[str], model: Optional[str] = None) -> List[List[float]]:
        """
        Compatibility alias for KB code that calls `openai_client.embed(texts)`.

        If model is None, we try to get it from app.settings.cfg (project settings).
        """
        if model is None:
            try:
                # local import to avoid circular deps at import time
                from app.settings import cfg  # type: ignore
                model = getattr(cfg, "OPENAI_EMBEDDING_MODEL", None)
            except Exception:
                model = None

        if not model:
            # safe default (matches your KB defaults)
            model = "text-embedding-3-large"

        return self.embeddings(texts, model=model)

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
        - For GPT-5.* we prefer the Responses API (recommended by OpenAI).
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
        prefer_responses = model.startswith("gpt-5") or model.startswith("o")

        if has_responses and (prefer_responses or True):
            last_err: Optional[Exception] = None
            for attempt in range(1, 4):
                try:
                    kwargs: Dict[str, Any] = {
                        "model": model,
                        "input": turns,
                    }
                    if instructions:
                        kwargs["instructions"] = instructions
                    if temperature is not None:
                        kwargs["temperature"] = float(temperature)
                    if max_output_tokens is not None:
                        kwargs["max_output_tokens"] = int(max_output_tokens)
                    if reasoning_effort:
                        kwargs["reasoning"] = {"effort": reasoning_effort}

                    resp = self.client.responses.create(**kwargs)
                    return _extract_output_text(resp)
                except Exception as e:
                    last_err = e
                    time.sleep(0.5 * attempt)
            raise last_err or RuntimeError("responses.create() failed")

        # Fallback: Chat Completions API
        last_err2: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                resp = self.client.chat.completions.create(
                    model=model,
                    messages=messages,  # keep original messages
                    temperature=float(temperature),
                    max_tokens=int(max_output_tokens) if max_output_tokens is not None else None,
                )
                return (resp.choices[0].message.content or "").strip()
            except Exception as e:
                last_err2 = e
                time.sleep(0.5 * attempt)
        raise last_err2 or RuntimeError("chat.completions.create() failed")

    # -------- (internal helper; kept for backward compatibility) --------
    def _extract_output_text(self, resp: Any) -> str:
        out_txt = getattr(resp, "output_text", None)
        if out_txt:
            return str(out_txt).strip()
        return str(resp).strip()

    # -------- images --------
    def generate_image_url(self, *, prompt: str, model: str = "gpt-image-1", size: str = "1024x1024") -> str:
        resp = self.client.images.generate(model=model, prompt=prompt, size=size)
        data = getattr(resp, "data", None) or []
        if not data:
            raise RuntimeError("No image data returned")
        url = getattr(data[0], "url", None)
        if not url:
            raise RuntimeError("No image URL returned")
        return str(url)

    # -------- audio transcription --------
    def transcribe_bytes(self, *, audio_bytes: bytes, filename: str, model: str = "whisper-1") -> str:
        bio = BytesIO(audio_bytes)
        bio.name = filename
        resp = self.client.audio.transcriptions.create(model=model, file=bio)
        return str(getattr(resp, "text", "")).strip()

    def transcribe_file(self, *, file_path: str, model: str = "whisper-1") -> str:
        with open(file_path, "rb") as f:
            bio = BytesIO(f.read())
        bio.name = file_path.split("/")[-1]
        resp = self.client.audio.transcriptions.create(model=model, file=bio)
        return str(getattr(resp, "text", "")).strip()
