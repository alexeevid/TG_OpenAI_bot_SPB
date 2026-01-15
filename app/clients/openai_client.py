from __future__ import annotations

import logging
import os
import time
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Literal, Set

from openai import OpenAI

log = logging.getLogger(__name__)


def _mask_key(k: str) -> str:
    k = k or ""
    if len(k) <= 6:
        return "***"
    return f"{k[:2]}***{k[-2:]}"


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
        # ✅ Normalize key: strip whitespace/newlines; treat empty as None.
        clean_key: Optional[str] = None
        if api_key is not None:
            k = str(api_key).strip()
            clean_key = k if k else None

        # Keep the original key for diagnostics (might be None if OpenAI SDK reads it from env)
        self._api_key = clean_key

        # ✅ IMPORTANT:
        # - If clean_key is provided -> pass explicitly.
        # - If clean_key is None -> let OpenAI SDK read OPENAI_API_KEY from environment.
        if clean_key:
            log.info("OpenAIClient: using explicit api_key len=%d masked=%s", len(clean_key), _mask_key(clean_key))
            self.client = OpenAI(api_key=clean_key)
        else:
            env_key = (os.getenv("OPENAI_API_KEY") or "").strip()
            if env_key:
                log.info("OpenAIClient: using OPENAI_API_KEY from env len=%d masked=%s", len(env_key), _mask_key(env_key))
            else:
                log.warning("OpenAIClient: OPENAI_API_KEY is missing/empty in env; API calls will fail")
            self.client = OpenAI()

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
            picked = {m for m in s if m.startswith(("gpt-", "o"))}
            return sorted(picked)

        if kind == "image":
            picked = {m for m in s if ("image" in m) or ("dall" in m)}
            return sorted(picked)

        if kind == "transcribe":
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
        - Prefer Responses API
        - Fallback to Chat Completions
        """
        # (остальной код файла — без изменений)
        resp = self.client.responses.create(
            model=model,
            input=messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            reasoning={"effort": reasoning_effort} if reasoning_effort else None,
        )
        out_text = getattr(resp, "output_text", None)
        if out_text:
            return str(out_text)

        text = ""
        try:
            for item in getattr(resp, "output", []) or []:
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", "") == "output_text":
                        text += getattr(c, "text", "") or ""
        except Exception:
            pass

        return text.strip()

    def transcribe(self, audio_bytes: bytes, *, model: str) -> str:
        bio = BytesIO(audio_bytes)
        bio.name = "audio.ogg"
        tr = self.client.audio.transcriptions.create(model=model, file=bio)
        return str(getattr(tr, "text", "") or "").strip()

    def generate_image(self, prompt: str, *, model: str) -> str:
        r = self.client.images.generate(model=model, prompt=prompt)
        data = getattr(r, "data", None) or []
        if not data:
            raise RuntimeError("Empty image response")
        first = data[0]
        url = getattr(first, "url", None)
        if not url:
            raise RuntimeError("No image URL in response")
        return str(url)

    def generate_image_url(self, *, prompt: str, model: str, size: str = "1024x1024") -> str:
        r = self.client.images.generate(model=model, prompt=prompt, size=size)
        data = getattr(r, "data", None) or []
        if not data:
            raise RuntimeError("Empty image response")
        first = data[0]
        url = getattr(first, "url", None)
        if not url:
            raise RuntimeError("No image URL in response")
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
