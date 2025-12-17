"""Project settings (compat + best-practice defaults).

Goals:
- Preserve existing contract: from app.settings import load_settings
- Provide backward-compatible attribute names used across codebase (telegram_token, text_model, etc.)
- Cover Railway env vars (see screenshot) with safe defaults
- Avoid repeated AttributeError cascades by providing commonly used flags (enable_image_generation, etc.)
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional, Set, List


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


def _getenv_float(name: str, default: float) -> float:
    v = _getenv(name)
    if v is None:
        return default
    try:
        return float(v)
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


def _getenv_str_list(name: str) -> List[str]:
    v = _getenv(name, "")
    return [p.strip() for p in v.split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    # Required core
    telegram_bot_token: str
    openai_api_key: str
    database_url: str

    # Admin / access
    admin_chat_id: Optional[int] = None
    admin_user_ids: Set[int] = None  # type: ignore[assignment]
    allowed_user_ids: Set[int] = None  # type: ignore[assignment]

    # Language / UX
    bot_language: str = "ru"

    # Models & params
    openai_text_model: str = "gpt-4.5-turbo"
    openai_image_model: str = "gpt-image-1"
    openai_embedding_model: str = "text-embedding-3-large"
    openai_transcribe_model: str = "whisper-1"
    openai_temperature: float = 0.2
    max_context_tokens: int = 8000

    # Feature flags
    enable_image_generation: bool = False
    enable_web_search: bool = False
    web_search_provider: str = "disabled"  # e.g., "tavily"
    tavily_api_key: str = ""

    # KB / RAG
    chunk_size: int = 900
    chunk_overlap: int = 150
    max_kb_chunks: int = 6
    kb_debug: bool = False
    kb_sync_entrypoint: str = ""
    kb_sync_interval: int = 0  # seconds

    # Security / webhook (optional)
    webhook_domain: str = ""
    webhook_secret: str = ""

    # Yandex.Disk
    yandex_disk_token: str = ""
    yandex_root_path: str = ""

    # Misc
    rate_limit_per_min: int = 60
    log_level: str = "INFO"
    denylist_models: List[str] = None  # type: ignore[assignment]

    # ---- Backward-compatible aliases used by existing code ----
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


def load_settings() -> Settings:
    # Core
    telegram_bot_token = (_getenv("TELEGRAM_BOT_TOKEN", "") or _getenv("BOT_TOKEN", "") or "")
    openai_api_key = _getenv("OPENAI_API_KEY", "") or ""
    database_url = _getenv("DATABASE_URL", "") or _getenv("POSTGRES_DSN", "") or ""

    # Admin / access
    admin_chat_id = None
    admin_chat_id_raw = _getenv("ADMIN_CHAT_ID")
    if admin_chat_id_raw:
        try:
            admin_chat_id = int(admin_chat_id_raw)
        except ValueError:
            admin_chat_id = None

    admin_user_ids = _getenv_int_set("ADMIN_USER_IDS") or _getenv_int_set("ADMIN_IDS")
    allowed_user_ids = _getenv_int_set("ALLOWED_USER_IDS") or _getenv_int_set("ALLOWED_IDS")

    # Language
    bot_language = (_getenv("BOT_LANGUAGE", "ru") or "ru").lower()

    # Models (allow overrides from multiple env variable names)
    openai_text_model = (_getenv("OPENAI_TEXT_MODEL") or _getenv("OPENAI_MODEL") or _getenv("TEXT_MODEL") or "gpt-4.5-turbo")
    openai_image_model = (_getenv("OPENAI_IMAGE_MODEL") or _getenv("IMAGE_MODEL") or "gpt-image-1")
    openai_embedding_model = (_getenv("OPENAI_EMBEDDING_MODEL") or _getenv("EMBEDDING_MODEL") or "text-embedding-3-large")
    openai_transcribe_model = (_getenv("OPENAI_TRANSCRIBE_MODEL") or _getenv("TRANSCRIBE_MODEL") or "whisper-1")

    openai_temperature = _getenv_float("OPENAI_TEMPERATURE", 0.2)
    max_context_tokens = _getenv_int("MAX_CONTEXT_TOKENS", 8000)

    # Feature flags
    enable_image_generation = _getenv_bool("ENABLE_IMAGE_GENERATION", False)
    enable_web_search = _getenv_bool("ENABLE_WEB_SEARCH", False)
    web_search_provider = (_getenv("WEB_SEARCH_PROVIDER", "disabled") or "disabled").lower()
    tavily_api_key = _getenv("TAVILY_API_KEY", "") or ""

    # KB / RAG params
    chunk_size = _getenv_int("CHUNK_SIZE", 900)
    chunk_overlap = _getenv_int("CHUNK_OVERLAP", 150)
    max_kb_chunks = _getenv_int("MAX_KB_CHUNKS", 6)
    kb_debug = _getenv_bool("KB_DEBUG", False)
    kb_sync_entrypoint = _getenv("KB_SYNC_ENTRYPOINT", "") or ""
    kb_sync_interval = _getenv_int("KB_SYNC_INTERVAL", 0)

    # Webhook optional
    webhook_domain = _getenv("WEBHOOK_DOMAIN", "") or ""
    webhook_secret = _getenv("WEBHOOK_SECRET", "") or ""

    # Yandex.Disk
    yandex_disk_token = _getenv("YANDEX_DISK_TOKEN", "") or ""
    yandex_root_path = _getenv("YANDEX_ROOT_PATH", "") or ""

    # Misc
    rate_limit_per_min = _getenv_int("RATE_LIMIT_PER_MIN", 60)
    log_level = (_getenv("LOG_LEVEL", "INFO") or "INFO").upper()
    denylist_models = _getenv_str_list("DENYLIST_MODELS")

    return Settings(
        telegram_bot_token=telegram_bot_token,
        openai_api_key=openai_api_key,
        database_url=database_url,
        admin_chat_id=admin_chat_id,
        admin_user_ids=admin_user_ids,
        allowed_user_ids=allowed_user_ids,
        bot_language=bot_language,
        openai_text_model=openai_text_model,
        openai_image_model=openai_image_model,
        openai_embedding_model=openai_embedding_model,
        openai_transcribe_model=openai_transcribe_model,
        openai_temperature=openai_temperature,
        max_context_tokens=max_context_tokens,
        enable_image_generation=enable_image_generation,
        enable_web_search=enable_web_search,
        web_search_provider=web_search_provider,
        tavily_api_key=tavily_api_key,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        max_kb_chunks=max_kb_chunks,
        kb_debug=kb_debug,
        kb_sync_entrypoint=kb_sync_entrypoint,
        kb_sync_interval=kb_sync_interval,
        webhook_domain=webhook_domain,
        webhook_secret=webhook_secret,
        yandex_disk_token=yandex_disk_token,
        yandex_root_path=yandex_root_path,
        rate_limit_per_min=rate_limit_per_min,
        log_level=log_level,
        denylist_models=denylist_models,
    )


_SETTINGS: Optional[Settings] = None

def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


cfg = get_settings()

# Legacy module-level constants (for code that imports directly from settings.py)
OPENAI_TEXT_MODEL = cfg.openai_text_model
OPENAI_IMAGE_MODEL = cfg.openai_image_model
OPENAI_EMBEDDING_MODEL = cfg.openai_embedding_model
OPENAI_TRANSCRIBE_MODEL = cfg.openai_transcribe_model
OPENAI_TEMPERATURE = cfg.openai_temperature
MAX_CONTEXT_TOKENS = cfg.max_context_tokens
ENABLE_IMAGE_GENERATION = cfg.enable_image_generation
ENABLE_WEB_SEARCH = cfg.enable_web_search
WEB_SEARCH_PROVIDER = cfg.web_search_provider
RATE_LIMIT_PER_MIN = cfg.rate_limit_per_min
LOG_LEVEL = cfg.log_level
