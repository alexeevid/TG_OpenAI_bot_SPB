"""Project settings.

This module must satisfy existing imports/contracts in the repo:
    from .settings import load_settings

And existing attribute names used across the codebase (aliases), e.g.:
    cfg.telegram_token   (legacy)
    cfg.telegram_bot_token (new)
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
    # Canonical names (preferred)
    telegram_bot_token: str
    openai_api_key: str
    database_url: str

    # Models
    openai_text_model: str = "gpt-4.5-turbo"
    openai_image_model: str = "gpt-image-1"
    openai_embedding_model: str = "text-embedding-3-large"
    openai_transcribe_model: str = "whisper-1"

    # Features / limits
    enable_web_search: bool = False
    rate_limit_per_min: int = 60

    # Optional: admins
    admin_ids: Set[int] = None  # type: ignore[assignment]

    # ---- Backward-compatible attribute aliases (used by existing code) ----
    @property
    def telegram_token(self) -> str:
        return self.telegram_bot_token

    @property
    def openai_key(self) -> str:
        return self.openai_api_key

    @property
    def db_url(self) -> str:
        return self.database_url

    @property
    def text_model(self) -> str:
        return self.openai_text_model

    @property
    def image_model(self) -> str:
        return self.openai_image_model

    @property
    def embedding_model(self) -> str:
        return self.openai_embedding_model

    @property
    def transcribe_model(self) -> str:
        return self.openai_transcribe_model


def load_settings() -> Settings:
    """Load settings from environment variables."""
    telegram_bot_token = _getenv("TELEGRAM_BOT_TOKEN", "") or _getenv("BOT_TOKEN", "") or ""
    openai_api_key = _getenv("OPENAI_API_KEY", "") or ""
    database_url = _getenv("DATABASE_URL", "") or _getenv("POSTGRES_DSN", "") or ""

    # Allow overrides
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


# Module-level cached settings + legacy constants (some modules may import these directly)
_SETTINGS: Optional[Settings] = None

def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


cfg = get_settings()

OPENAI_TEXT_MODEL = cfg.openai_text_model
OPENAI_IMAGE_MODEL = cfg.openai_image_model
OPENAI_EMBEDDING_MODEL = cfg.openai_embedding_model
OPENAI_TRANSCRIBE_MODEL = cfg.openai_transcribe_model
ENABLE_WEB_SEARCH = cfg.enable_web_search
RATE_LIMIT_PER_MIN = cfg.rate_limit_per_min
