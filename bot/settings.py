# bot/settings.py
from __future__ import annotations

import json
import os
import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


def _split_commas_spaces(s: str) -> List[str]:
    # Разбиваем по запятым и/или пробелам, игнорируя пустые элементы
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

    # Model filters (optional)
    allowed_models_whitelist: Optional[List[str]] = Field(default=None, alias="ALLOWED_MODELS_WHITELIST")
    models_blacklist: Optional[List[str]] = Field(default=None, alias="MODELS_BLACKLIST")

    # ---- ВАЛИДАТОРЫ: списки ID (int) ----
    @field_validator("admin_user_ids", "admin_set", "allowed_user_ids", "allowed_set", mode="before")
    @classmethod
    def _parse_id_list(cls, v):
        """
        Поддерживаем форматы:
        - список Python/JSON: [123, 456] или ["123","456"]
        - строка: "123, 456" или "123 456" или "123"
        - уже готовый list[int]
        """
        if v is None:
            return None
        if isinstance(v, list):
            # Приводим элементы к int
            out = []
            for x in v:
                if x is None or (isinstance(x, str) and not x.strip()):
                    continue
                out.append(int(x))
            return out or None
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            # Попробуем как JSON
            if s.startswith("[") and s.endswith("]"):
                try:
                    arr = json.loads(s)
                    if not isinstance(arr, list):
                        return None
                    out = []
                    for x in arr:
                        if x is None or (isinstance(x, str) and not x.strip()):
                            continue
                        out.append(int(x))
                    return out or None
                except Exception:
                    # упадем в разбор по запятым/пробелам
                    pass
            # Комма/пробел разделители
            parts = _split_commas_spaces(s)
            if not parts:
                return None
            try:
                return [int(p) for p in parts]
            except Exception:
                # Последняя попытка: одна строка-число
                return [int(s)]
        # Иной тип — не поддерживаем
        return None

    # ---- ВАЛИДАТОРЫ: списки моделей (str) ----
    @field_validator("allowed_models_whitelist", "models_blacklist", mode="before")
    @classmethod
    def _parse_str_list(cls, v):
        """
        Поддерживаем:
        - JSON-массив строк
        - строка с запятыми / пробелами
        - list[str]
        """
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
                    if not isinstance(arr, list):
                        return None
                    return [str(x).strip() for x in arr if str(x).strip()]
                except Exception:
                    pass
            return [p for p in _split_commas_spaces(s) if p]
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
        if v.startswith("postgresql://") and "+psycopg2://" not in v:
            return v.replace("postgresql://", "postgresql+psycopg2://", 1)
        return v
