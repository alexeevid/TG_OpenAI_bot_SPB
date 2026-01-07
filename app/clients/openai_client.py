from __future__ import annotations

import logging
import time
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Literal, Set

from openai import OpenAI

log = logging.getLogger(__name__)

ModelKind = Literal["text", "image", "transcribe", "embeddings"]


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
        # Keep the original key (might be None if OpenAI SDK reads it from env)
        self._api_key = api_key
        self.client = OpenAI(api_key=api_key)

        # --- models cache (per API key) ---
        self._models_cache: Optional[Set[str]] = None
        self._models_cache_ts: float = 0.0
        self._models_cache_ttl_sec: int = 1800  # 30 minutes

    def is_enabled(self) -> bool:
        """
        Backward-compatible feature flag used by older KB/RAG code.

        Returns True if we likely have an API key configured (explicitly or via env).
        Even if True, actual calls can still fail; callers should keep try/except.
        """
        if self._api_key:
            return True

        # Try to introspect common OpenAI SDK internal fields (best-effort)
        for attr_path in (
            ("api_key",),
            ("_client", "api_key"),
            ("_client", "_config", "api_key"),
        ):
            obj: Any = self.client
            ok = True
            for a in attr_path:
                if not hasattr(obj, a):
                    ok = False
                    break
                obj = getattr(obj, a)
            if ok and obj:
                return True

        return False

    # -------- models --------
    def _list_models_cached(self, *, force_refresh: bool = False) -> List[str]:
        """
        Internal helper that returns available model ids for THIS API key.
        Uses TTL cache to reduce API calls.
        """
        now = time.time()

        if (
            not force_refresh
            and self._models_cache is not None
            and (now - self._models_cache_ts) < self._models_cache_ttl_sec
        ):
            return sorted(self._models_cache)

        try:
            out = self.client.models.list()
            ids: Set[str] = set()
            for m in getattr(out, "data", []) or []:
                mid = getattr(m, "id", None)
                if mid:
                    ids.add(str(mid))

            self._models_cache = ids
            self._models_cache_ts = now

            log.info("OpenAI available models (%d): %s", len(ids), sorted(ids))
            return sorted(ids)
        except Exception as e:
            log.warning("Failed to list models: %s", e)

            # If listing fails but we have an older cache, keep using it.
            if self._models_cache is not None:
                return sorted(self._models_cache)

            return []

    def list_models(self) -> List[str]:
        """
        Backward-compatible public method.
        Returns all available model ids for THIS API key.
        """
        return self._list_models_cached(force_refresh=False)

    def list_models_by_kind(self, kind: ModelKind, *, force_refresh: bool = False) -> List[str]:
        """
        Return models filtered by a coarse "kind" (modality family).
        NOTE: This is heuristic-based (by model id naming). Still safe because
        actual calls should be guarded by try/except and/or ensure_model_available().
        """
        all_models = self._list_models_cached(force_refresh=force_refresh)
        s = set(all_models)

        if kind == "text":
            # Text / reasoning families commonly used in Responses & ChatCompletions.
            # Keep it conservative: if model is available and starts with known prefixes.
            picked = {m for m in s if m.startswith(("gpt-", "o"))}
            return sorted(picked)

        if kind == "image":
            # Image generation families.
            picked = {m for m in s if ("image" in m) or ("dall" in m)}
            return sorted(picked)

        if kind == "transcribe":
            # Speech-to-text families.
            picked = {m for m in s if ("whisper" in m) or ("transcribe" in m)}
            return sorted(picked)

        if kind == "embeddings":
            picked = {m for m in s if "embedding" in m}
            return sorted(picked)

        return []

    def ensure_model_available(
        self,
        *,
        model: Optional[str],
        kind: ModelKind,
        fallback: str,
        force_refresh: bool = False,
    ) -> str:
        """
        If `model` is empty or not available (for this API key / kind), return fallback.
        """
        if not model:
            return fallback

        available = set(self.list_models_by_kind(kind, force_refresh=force_refresh))
        if model in available:
            return model

        log.warning(
            "Model '%s' not available for kind=%s. Fallback to '%s'. Available(%d): %s",
            model,
            kind,
            fallback,
            len(available),
            sorted(available),
        )
        return fallback

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

        # Optional: validate embedding model against available models list (non-fatal)
        try:
            model = self.ensure_model_available(
                model=model,
                kind="embeddings",
                fallback=model,
            )
        except Exception:
            pass

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
            log.warning(
                "Responses API failed for model=%s, falling back to chat.completions. err=%s",
                model,
                last_err,
            )

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
        # Validate model gently: if not available, keep default.
        model = self.ensure_model_available(model=model, kind="image", fallback="gpt-image-1")

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
        # Validate model gently: if not available, keep default.
        model = self.ensure_model_available(model=model, kind="transcribe", fallback="whisper-1")

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
