# bot/settings.py (добавим явный вывод debug-логов при старте)

from __future__ import annotations
import json, os, re
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


def _split_commas_spaces(s: str) -> List[str]:
    return [part for part in re.split(r"[,\s]+", s.strip()) if part]


class Settings(BaseSettings):
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")
    bot_language: Optional[str] = Field(default=None, alias="BOT_LANGUAGE")
    image_model: str = Field(default="gpt-image-1", alias="IMAGE_MODEL")
    enable_image_generation: bool = Field(default=True, alias="ENABLE_IMAGE_GENERATION")

    # --- Access ---
    admin_user_ids: Optional[List[int]] = Field(default=None, alias="ADMIN_USER_IDS")
    allowed_user_ids: Optional[List[int]] = Field(default=None, alias="ALLOWED_USER_IDS")
    allowed_models_whitelist: Optional[List[str]] = Field(default=None, alias="ALLOWED_MODELS_WHITELIST")

    # --- KB ---
    yandex_disk_token: Optional[str] = Field(default=None, alias="YANDEX_DISK_TOKEN")
    yandex_root_path: str = Field(default="База Знаний", alias="YANDEX_ROOT_PATH")
    kb_debug: Optional[bool] = Field(default=False, alias="KB_DEBUG")
    kb_sync_interval: Optional[int] = Field(default=300, alias="KB_SYNC_INTERVAL")

    # --- Embeddings ---
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")
    chunk_size: int = Field(default=400, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=80, alias="CHUNK_OVERLAP")
    max_kb_chunks: int = Field(default=25, alias="MAX_KB_CHUNKS")

    # --- DB ---
    database_url: Optional[str] = Field(default=None, alias="DATABASE_URL")
    postgres_url: Optional[str] = Field(default=None, alias="POSTGRES_URL")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("admin_user_ids", "allowed_user_ids", mode="before")
    @classmethod
    def _parse_id_list(cls, v):
        if v is None:
            return None
        if isinstance(v, list):
            return [int(x) for x in v if x]
        if isinstance(v, str):
            try:
                if v.startswith("["):
                    return [int(x) for x in json.loads(v)]
                return [int(x) for x in _split_commas_spaces(v)]
            except Exception:
                return None
        return None
