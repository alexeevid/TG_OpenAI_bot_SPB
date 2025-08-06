from __future__ import annotations

import json
import os
import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


def _split_commas_spaces(s: str) -> List[str]:
    return [part for part in re.split(r"[,\s]+", s.strip()) if part]


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    bot_language: Optional[str] = Field(default=None, alias="BOT_LANGUAGE")

    # OpenAI
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")
    openai_temperature: float = Field(default=0.2, alias="OPENAI_TEMPERATURE")

    # Image generation
    image_model: str = Field(default="gpt-image-1", alias="IMAGE_MODEL")
    enable_image_generation: bool = Field(default=True, alias="ENABLE_IMAGE_GENERATION")

    # Access control (lists of user IDs)
    admin_user_ids: Optional[List[int]] = Field(default=None, alias="ADMIN_USER_IDS")
    admin_set: Optional[List[int]] = Field(default=None, alias="ADMIN_SET")
    allowed_user_ids: Optional[List[int]] = Field(default=None, alias="ALLOWED_USER_IDS")
    allowed_set: Optional[List[int]] = Field(default=None, alias="ALLOWED_SET")

    # Yandex Disk & KB
    yandex_disk_token: Optional[str] = Field(default=None, alias="YANDEX_DISK_TOKEN")
    yandex_root_path: str = Field(default="disk:/База Знаний", alias="YANDEX_ROOT_PATH")
    yandex_disk_folder: str = Field(default="База Знаний", alias="YANDEX_DISK_FOLDER")

    # KB: embeddings & retrieval
    kb_embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")
    kb_top_k: int = Field(default=6, alias="KB_TOP_K")
    chunk_size: int = Field(default=500, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=50, alias="CHUNK_OVERLAP")
    max_kb_chunks: int = Field(default=30, alias="MAX_KB_CHUNKS")
    kb_sync_interval: int = Field(default=900, alias="KB_SYNC_INTERVAL")
    kb_debug: bool = Field(default=False, alias="KB_DEBUG")

    # Optional: Vector DB for embeddings
    kb_vector_db_url: Optional[str] = Field(default=None, alias="KB_VECTOR_DB_URL")

    # Optional: model filters
    allowed_models_whitelist: Optional[List[str]] = Field(default=None, alias="ALLOWED_MODELS_WHITELIST")
    models_blacklist: Optional[List[str]] = Field(default=None, alias="DENYLIST_MODELS")

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Optional: for compatibility with legacy systems
    database_url: Optional[str] = Field(default=None, alias="DATABASE_URL")
    postgres_url: Optional[str] = Field(default=None, alias="POSTGRES_URL")

    # 🔍 Validators for ID lists
    @field_validator("admin_user_ids", "admin_set", "allowed_user_ids", "allowed_set", mode="before")
    @classmethod
    def _parse_id_list(cls, v):
        if v is None:
            return None
        if isinstance(v, list):
            return [int(x) for x in v if x is not None and str(x).strip()]
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            if s.startswith("[") and s.endswith("]"):
                try:
                    arr = json.loads(s)
                    return [int(x) for x in arr if x is not None and str(x).strip()]
                except Exception:
                    pass
            parts = _split_commas_spaces(s)
            try:
                return [int(p) for p in parts]
            except Exception:
                return [int(s)]
        return None

    # 🔍 Validators for model name lists
    @field_validator("allowed_models_whitelist", "models_blacklist", mode="before")
    @classmethod
    def _parse_str_list(cls, v):
        if v is None:
            return None
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            if s.startswith("[") and s.endswith("]"):
                try:
                    arr = json.loads(s)
                    return [str(x).strip() for x in arr if str(x).strip()]
                except Exception:
                    pass
            return [p for p in _split_commas_spaces(s) if p]
        return None

    # 🔍 Normalize PostgreSQL DSN
    @field_validator("kb_vector_db_url", "database_url", "postgres_url", mode="after")
    @classmethod
    def _normalize_pg_url(cls, v):
        if not v:
            return v
        if v.startswith("postgres://"):
            return "postgresql+psycopg2://" + v[len("postgres://"):]
        if v.startswith("postgresql://") and "+psycopg2://" not in v:
            return v.replace("postgresql://", "postgresql+psycopg2://", 1)
        return v
