from __future__ import annotations

import logging
import time
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence

from openai import OpenAI

log = logging.getLogger(__name__)


class OpenAIClient:
    """
    OpenAI API client wrapper.

    We use:
    - text generation (try Responses API; fallback to Chat Completions when needed)
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
            ids: List[str] = []
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
        if not texts:
            return []

        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                resp = self.client.embeddings.create(model=model, input=list(texts))
                return [d.embedding for d in resp.data]
            except Exception as e:
                last_err = e
                time.sleep(0.5 * attempt)

        raise last_err or RuntimeError("embeddings() failed")

    def embed(self, texts: Sequence[str], model: Optional[str] = None) -> List[List[float]]:
        # Backward-compatible alias
        if model is None:
            try:
                from app.settings import cfg  # type: ignore
                model = getattr(cfg, "OPENAI_EMBEDDING_MODEL", None)
            except Exception:
                model = None

        if not model:
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
        """
        Generate assistant text.

        Strategy:
        1) Try Responses API when available and likely supported by the model.
           If it fails with "unsupported/unknown" style errors -> fallback.
        2) Fallback to Chat Completions.

        This makes text generation resilient across model families/accounts.
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

        def _should_try_responses(model_name: str) -> bool:
            # Conservative allowlist: models most likely to support Responses well.
            # (We still fallback if the API rejects it.)
            return model_name.startswith(("gpt-5", "o", "gpt-4o", "gpt-4.1"))

        has_responses = hasattr(self.client, "responses") and hasattr(self.client.responses, "create")

        # 1) Try Responses (if available + model family suggests it)
        if has_responses and _should_try_responses(model):
            last_err: Optional[Exception] = None
            for attempt in range(1, 3 + 1):
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
                    msg = str(e).lower()
                    # If model/endpoint is not supported, break to fallback immediately.
                    if any(
                        s in msg
                        for s in (
                            "unsupported",
                            "not supported",
                            "unknown model",
                            "model_not_found",
                            "invalid model",
                            "404",
                            "not found",
                            "unrecognized",
                            "responses",
                        )
                    ):
                        break
                    time.sleep(0.5 * attempt)

            # If we tried Responses and it failed, we continue to fallback below.
            log.warning("Responses API failed for model=%s, falling back to chat.completions. err=%s", model, last_err)

        # 2) Fallback: Chat Completions API
        last_err2: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                resp = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=float(temperature),
                    max_tokens=int(max_output_tokens) if max_output_tokens is not None else None,
                )
                return (resp.choices[0].message.content or "").strip()
            except Exception as e:
                last_err2 = e
                time.sleep(0.5 * attempt)

        raise last_err2 or RuntimeError("chat.completions.create() failed")

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
    def transcribe_file(self, file_obj, model: str = "whisper-1") -> str:
        """
        Transcribe from an already opened binary file-like object.
        (This is what VoiceService currently uses.)
        """
        resp = self.client.audio.transcriptions.create(model=model, file=file_obj)
        return str(getattr(resp, "text", "")).strip()

    def transcribe_bytes(self, *, audio_bytes: bytes, filename: str, model: str = "whisper-1") -> str:
        bio = BytesIO(audio_bytes)
        bio.name = filename
        return self.transcribe_file(bio, model=model)

    def transcribe_path(self, *, file_path: str, model: str = "whisper-1") -> str:
        """
        Transcribe from a filesystem path.
        """
        with open(file_path, "rb") as f:
            return self.transcribe_file(f, model=model)
