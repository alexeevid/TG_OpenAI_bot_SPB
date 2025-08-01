# bot/config.py
from __future__ import annotations

import json
import os
import re
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


def _parse_csv_ints(s: str | None) -> List[int]:
    """Парсит '1, 2  3' -> [1,2,3]. Пустые значения игнорируются."""
    if not s:
        return []
    parts = [p.strip() for p in re.split(r"[,\s]+", s) if p.strip()]
    out: List[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except Exception:
            # игнорируем мусор
            pass
    return out


def _parse_json_or_csv_to_ints(v: str | None) -> List[int]:
    """Пробуем как JSON, если не вышло — как CSV."""
    if not v:
        return []
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        try:
            data = json.loads(v)
            if isinstance(data, list):
                return [int(x) for x in data]
        except Exception:
            pass
    return _parse_csv_ints(v)


def _parse_csv_strs(s: str | None) -> List[str]:
    if not s:
        return []
    return [p.strip() for p in re.split(r"[,\s]+", s) if p.strip()]


def _to_bool(v: str | bool | int | None) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _normalize_db_url(url: str | None) -> str | None:
    if not url:
        return url
    url = url.strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://") and not url.startswith("postgresql+psycopg2://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


class Settings(BaseSettings):
    # --- Основное ---
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")

    bot_language: Optional[str] = Field(default=None, alias="BOT_LANGUAGE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- Доступ пользователей ---
    # ВАЖНО: храним "raw" строку из окружения и превращаем её в список через свойства.
    admin_user_ids_env: Optional[str] = Field(default=None, alias="ADMIN_USER_IDS")
    allowed_user_ids_env: Optional[str] = Field(default=None, alias="ALLOWED_USER_IDS")

    # --- Модели ---
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")
    image_model: str = Field(default="gpt-image-1", alias="IMAGE_MODEL")
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")

    allowed_models_whitelist_env: Optional[str] = Field(default=None, alias="ALLOWED_MODELS_WHITELIST")
    denylist_models_env: Optional[str] = Field(default=None, alias="DENYLIST_MODELS")

    enable_image_generation_env: Optional[str] = Field(default=None, alias="ENABLE_IMAGE_GENERATION")

    # --- База данных / Railway ---
    database_url_env: Optional[str] = Field(default=None, alias="DATABASE_URL")
    postgres_url_env: Optional[str] = Field(default=None, alias="POSTGRES_URL")

    # --- База знаний / Яндекс.Диск ---
    yandex_disk_token: Optional[str] = Field(default=None, alias="YANDEX_DISK_TOKEN")
    yandex_root_path: Optional[str] = Field(default=None, alias="YANDEX_ROOT_PATH")

    kb_sync_interval_env: Optional[str] = Field(default=None, alias="KB_SYNC_INTERVAL")
    openai_temperature_env: Optional[str] = Field(default=None, alias="OPENAI_TEMPERATURE")

    # ===== Валидаторы простых нормализаций (там, где тип уже строка) =====
    @field_validator("database_url_env", "postgres_url_env", mode="before")
    @classmethod
    def _v_norm_db(cls, v):
        return _normalize_db_url(v)

    # ===== Удобные свойства (всё приводим к ожидаемым типам) =====
    @property
    def admin_user_ids(self) -> List[int]:
        return _parse_json_or_csv_to_ints(self.admin_user_ids_env)

    @property
    def allowed_user_ids(self) -> List[int]:
        return _parse_json_or_csv_to_ints(self.allowed_user_ids_env)

    @property
    def allowed_models_whitelist(self) -> List[str]:
        return _parse_csv_strs(self.allowed_models_whitelist_env)

    @property
    def denylist_models(self) -> List[str]:
        return _parse_csv_strs(self.denylist_models_env)

    @property
    def enable_image_generation(self) -> bool:
        return _to_bool(self.enable_image_generation_env)

    @property
    def kb_sync_interval(self) -> int:
        try:
            return int(self.kb_sync_interval_env) if self.kb_sync_interval_env else 0
        except Exception:
            return 0

    @property
    def openai_temperature(self) -> float:
        try:
            return float(self.openai_temperature_env) if self.openai_temperature_env else 0.2
        except Exception:
            return 0.2

    @property
    def effective_database_url(self) -> Optional[str]:
        """Приоритет: DATABASE_URL, затем POSTGRES_URL (оба нормализованы)."""
        return self.database_url_env or self.postgres_url_env

    # Совместимость с альтернативными именами (если где-то в коде смотрели YADISK_*)
    @property
    def yadisk_token(self) -> Optional[str]:
        return self.yandex_disk_token or os.getenv("YADISK_TOKEN")

    @property
    def yadisk_folder(self) -> Optional[str]:
        return self.yandex_root_path or os.getenv("YADISK_FOLDER")

    class Config:
        extra = "ignore"
        populate_by_name = True


def load_settings() -> Settings:
    return Settings()
