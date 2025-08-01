# bot/config.py
from __future__ import annotations

import os
import re
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


def _parse_csv_ints(v: str | List[int] | None) -> List[int]:
    """
    Принимает "540532439, 927603419" | ["540532439","927603419"] | [540532439, 927603419]
    и возвращает List[int]. Пустые/пробелы игнорируются.
    """
    if v is None:
        return []
    if isinstance(v, list):
        out: List[int] = []
        for item in v:
            try:
                out.append(int(item))
            except Exception:
                pass
        return out
    # строка
    parts = [p.strip() for p in re.split(r"[,\s]+", v) if p.strip()]
    out: List[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except Exception:
            pass
    return out


def _parse_csv_strs(v: str | List[str] | None) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    parts = [p.strip() for p in re.split(r"[,\s]+", v) if p.strip()]
    return parts


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
    if url.startswith("postgresql://"):
        # Явно добавим драйвер, если его нет
        if not url.startswith("postgresql+psycopg2://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


class Settings(BaseSettings):
    # --- Основное ---
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")

    # Язык бота (например, "ru"); опционально
    bot_language: Optional[str] = Field(default=None, alias="BOT_LANGUAGE")

    # Логи
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- Доступ пользователей ---
    # Списки можно задавать строкой "id1, id2" — парсим в List[int]
    admin_user_ids: List[int] = Field(default_factory=list, alias="ADMIN_USER_IDS")
    allowed_user_ids: List[int] = Field(default_factory=list, alias="ALLOWED_USER_IDS")

    # --- Модели ---
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")
    image_model: str = Field(default="gpt-image-1", alias="IMAGE_MODEL")
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")

    # Ограничение доступных моделей (whitelist) и denylist — не обязательно
    allowed_models_whitelist: List[str] = Field(default_factory=list, alias="ALLOWED_MODELS_WHITELIST")
    denylist_models: List[str] = Field(default_factory=list, alias="DENYLIST_MODELS")

    # Генерация изображений
    enable_image_generation: bool = Field(default=True, alias="ENABLE_IMAGE_GENERATION")

    # --- База данных / хранилище ---
    # Railway может давать POSTGRES_URL; ваш конфиг также содержит DATABASE_URL
    database_url: Optional[str] = Field(default=None, alias="DATABASE_URL")
    postgres_url: Optional[str] = Field(default=None, alias="POSTGRES_URL")

    # --- База знаний / Яндекс.Диск ---
    # По скрину у вас YANDEX_DISK_TOKEN / YANDEX_ROOT_PATH; в коде ранее встречались YADISK_TOKEN / YADISK_FOLDER.
    # Поддержим оба варианта через свойства (см. ниже).
    yandex_disk_token: Optional[str] = Field(default=None, alias="YANDEX_DISK_TOKEN")
    yandex_root_path: Optional[str] = Field(default=None, alias="YANDEX_ROOT_PATH")

    # Интервал авто-синка (если используется) — в минутах
    kb_sync_interval: int = Field(default=0, alias="KB_SYNC_INTERVAL")

    # Параметры генерирования
    openai_temperature: float = Field(default=0.2, alias="OPENAI_TEMPERATURE")

    # ===== Validators / пост-обработка =====
    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def _v_admin_ids(cls, v):
        return _parse_csv_ints(v)

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _v_allowed_ids(cls, v):
        return _parse_csv_ints(v)

    @field_validator("allowed_models_whitelist", "denylist_models", mode="before")
    @classmethod
    def _v_models_lists(cls, v):
        return _parse_csv_strs(v)

    @field_validator("enable_image_generation", mode="before")
    @classmethod
    def _v_enable_image(cls, v):
        return _to_bool(v)

    @field_validator("database_url", "postgres_url", mode="before")
    @classmethod
    def _v_norm_db(cls, v):
        return _normalize_db_url(v)

    # ===== Совместимость и удобные свойства =====
    @property
    def effective_database_url(self) -> Optional[str]:
        """
        Единый URL БД: приоритет DATABASE_URL, затем POSTGRES_URL.
        Всегда нормализован до postgresql+psycopg2://
        """
        return self.database_url or self.postgres_url

    @property
    def yadisk_token(self) -> Optional[str]:
        """
        Единый способ получить токен Я.Диска.
        Поддерживает как YANDEX_DISK_TOKEN (из вашего скрина), так и возможный YADISK_TOKEN.
        """
        return self.yandex_disk_token or os.getenv("YADISK_TOKEN")

    @property
    def yadisk_folder(self) -> Optional[str]:
        """
        Единый способ получить корневой путь папки БЗ.
        Поддерживает YANDEX_ROOT_PATH (из вашего скрина) и возможный YADISK_FOLDER.
        Рекомендуемый формат: 'disk:/База Знаний'
        """
        return self.yandex_root_path or os.getenv("YADISK_FOLDER")

    class Config:
        extra = "ignore"
        populate_by_name = True  # разрешает использовать как алиасы, так и имена полей напрямую


def load_settings() -> Settings:
    """
    Единая точка загрузки настроек.
    """
    s = Settings()

    # Если вдруг кто-то всё ещё пользуется переменными старого формата,
    # здесь можно сделать «мягкий» бридж — но основное уже закрыто свойствами.
    return s
