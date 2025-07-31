# bot/settings.py
from __future__ import annotations

import json
import os
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


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

    # Access control
    admin_user_ids: Optional[List[int]] = Field(default=None, alias="ADMIN_USER_IDS")
    admin_set: Optional[List[int]] = Field(default=None, alias="ADMIN_SET")
    allowed_user_ids: Optional[List[int]] = Field(default=None, alias="ALLOWED_USER_IDS")
    allowed_set: Optional[List[int]] = Field(default=None, alias="ALLOWED_SET")

    # KB / Yandex Disk
    yandex_disk_token: Optional[str] = Field(default=None, alias="YANDEX_DISK_TOKEN")
    yandex_disk_folder: str = Field(default="База Знаний", alias="YANDEX_DISK_FOLDER")

    # KB / embeddings & retrieval
    kb_embedding_model: str = Field(default="text-embedding-3-small", alias="KB_EMBEDDING_MODEL")
    kb_top_k: int = Field(default=6, alias="KB_TOP_K")

    # Vector DB (optional)
    kb_vector_db_url: Optional[str] = Field(default=None, alias="KB_VECTOR_DB_URL")

    # Model filters (optional JSON)
    allowed_models_whitelist: Optional[List[str]] = Field(default=None, alias="ALLOWED_MODELS_WHITELIST")
    models_blacklist: Optional[List[str]] = Field(default=None, alias="MODELS_BLACKLIST")

    @field_validator("allowed_models_whitelist", "models_blacklist", mode="before")
    @classmethod
    def _parse_json_array(cls, v):
        if v is None:
            return None
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            try:
                return json.loads(s)
            except Exception:
                # Если пришла не-JSON строка — трактуем как один элемент
                return [s]
        return None

    @field_validator("kb_vector_db_url", mode="after")
    @classmethod
    def _normalize_pg_url(cls, v):
        """
        Приводим postgres://... к postgresql+psycopg2://...
        """
        if not v:
            return v
        if v.startswith("postgres://"):
            return "postgresql+psycopg2://" + v[len("postgres://") :]
        if v.startswith("postgresql://"):
            # добавим psycopg2, если не указан другой драйвер
            if "+psycopg2://" not in v:
                return v.replace("postgresql://", "postgresql+psycopg2://", 1)
        return v
