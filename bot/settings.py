from pydantic_settings import BaseSettings
from pydantic import Field
from dotenv import load_dotenv
import os

load_dotenv()


class Settings(BaseSettings):
    database_url: str = Field(..., alias="DATABASE_URL")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o", alias="OPENAI_MODEL")
    image_model: str | None = Field(None, alias="IMAGE_MODEL")
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    allowed_set: list[int] = Field(default_factory=list, alias="ALLOWED_TELEGRAM_USER_IDS")
    admin_set: list[int] = Field(default_factory=list, alias="ADMIN_USER_IDS")
    yandex_disk_token: str | None = Field(None, alias="YANDEX_DISK_TOKEN")
    yandex_root_path: str | None = Field(None, alias="YANDEX_ROOT_PATH")

    class Config:
        env_file = ".env"
        populate_by_name = True


settings = Settings()

# Диагностика
print("\n🔍 DEBUG: Проверка структуры settings\n")

print(f"➡️ hasattr(settings, 'log_level'): {hasattr(settings, 'log_level')}")
print(f"➡️ getattr(settings, 'log_level', 'NOT FOUND'): {getattr(settings, 'log_level', 'NOT FOUND')}")
print(f"➡️ model_fields: {list(settings.model_fields.keys())}")
print(f"➡️ model_dump: {settings.model_dump()}")

print("\n✅ Конец диагностики settings.py\n")
