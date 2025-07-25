# bot/config.py
from typing import List, Union
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator

def _parse_id_list(value: Union[str, int, List[int], None]) -> List[int]:
    if value is None:
        return []
    if isinstance(value, list):
        # уже список – убедимся, что элементы int
        return [int(v) for v in value]
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        # строка "1,2,3" -> [1,2,3]
        value = value.strip()
        if not value:
            return []
        return [int(v.strip()) for v in value.split(",")]
    return []

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    # Telegram / OpenAI
    TELEGRAM_BOT_TOKEN: str
    OPENAI_API_KEY: str
    OPENAI_MODEL: str = "gpt-4o-mini"
    IMAGE_MODEL: str = "gpt-image-1"

    # Яндекс.Диск (REST)
    YANDEX_DISK_TOKEN: str = ""
    YANDEX_ROOT_PATH: str = "/"

    # База
    DATABASE_URL: str = "sqlite:///./local.db"

    # Доступы
    ADMIN_USER_IDS: List[int] = Field(default_factory=list)
    ALLOWED_TELEGRAM_USER_IDS: List[int] = Field(default_factory=list)

    # Логи
    LOG_LEVEL: str = "INFO"

    @field_validator("ADMIN_USER_IDS", mode="before")
    @classmethod
    def _val_admins(cls, v):
        return _parse_id_list(v)

    @field_validator("ALLOWED_TELEGRAM_USER_IDS", mode="before")
    @classmethod
    def _val_allowed(cls, v):
        return _parse_id_list(v)

    # Удобные множества
    @property
    def admin_set(self) -> set[int]:
        return set(self.ADMIN_USER_IDS)

    @property
    def allowed_set(self) -> set[int]:
        # если список пуст — разрешаем всем (или наоборот, можно вернуть пустой set)
        return set(self.ALLOWED_TELEGRAM_USER_IDS)

def load_settings() -> Settings:
    return Settings()
