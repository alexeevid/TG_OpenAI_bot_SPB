"""Project settings.

This file preserves the contract expected by app.main:
    from .settings import load_settings

It uses a lightweight dataclass-based loader (no pydantic dependency),
reading values from environment variables.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional, Set


def _getenv(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _getenv_int(name: str, default: int) -> int:
    v = _getenv(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _getenv_bool(name: str, default: bool = False) -> bool:
    v = _getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _getenv_int_set(name: str) -> Set[int]:
    v = _getenv(name, "")
    out: Set[int] = set()
    for part in v.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    openai_api_key: str
    database_url: str

    openai_text_model: str = "gpt-4.5-turbo"
    openai_image_model: str = "gpt-image-1"
    openai_embedding_model: str = "text-embedding-3-large"
    openai_transcribe_model: str = "whisper-1"

    enable_web_search: bool = False
    rate_limit_per_min: int = 60

    admin_ids: Set[int] = None  # type: ignore[assignment]


def load_settings() -> Settings:
    telegram_bot_token = _getenv("TELEGRAM_BOT_TOKEN", "") or _getenv("BOT_TOKEN", "") or ""
    openai_api_key = _getenv("OPENAI_API_KEY", "") or ""
    database_url = _getenv("DATABASE_URL", "") or _getenv("POSTGRES_DSN", "") or ""

    openai_text_model = _getenv("OPENAI_TEXT_MODEL", _getenv("OPENAI_MODEL", "gpt-4.5-turbo")) or "gpt-4.5-turbo"
    openai_image_model = _getenv("OPENAI_IMAGE_MODEL", "gpt-image-1") or "gpt-image-1"
    openai_embedding_model = _getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large") or "text-embedding-3-large"
    openai_transcribe_model = _getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1") or "whisper-1"

    enable_web_search = _getenv_bool("ENABLE_WEB_SEARCH", False)
    rate_limit_per_min = _getenv_int("RATE_LIMIT_PER_MIN", 60)
    admin_ids = _getenv_int_set("ADMIN_IDS")

    return Settings(
        telegram_bot_token=telegram_bot_token,
        openai_api_key=openai_api_key,
        database_url=database_url,
        openai_text_model=openai_text_model,
        openai_image_model=openai_image_model,
        openai_embedding_model=openai_embedding_model,
        openai_transcribe_model=openai_transcribe_model,
        enable_web_search=enable_web_search,
        rate_limit_per_min=rate_limit_per_min,
        admin_ids=admin_ids,
    )


_SETTINGS = None

def _settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


OPENAI_TEXT_MODEL = _settings().openai_text_model
OPENAI_IMAGE_MODEL = _settings().openai_image_model
OPENAI_EMBEDDING_MODEL = _settings().openai_embedding_model
OPENAI_TRANSCRIBE_MODEL = _settings().openai_transcribe_model
ENABLE_WEB_SEARCH = _settings().enable_web_search
RATE_LIMIT_PER_MIN = _settings().rate_limit_per_min
