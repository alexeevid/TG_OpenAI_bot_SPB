from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List

class Settings(BaseSettings):
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")
    vision_model: str = Field("gpt-4o", alias="VISION_MODEL")
    image_model: str = Field("gpt-image-1", alias="IMAGE_MODEL")
    tts_model: str = Field("gpt-4o-mini-tts", alias="TTS_MODEL")
    bot_language: str = Field("ru", alias="BOT_LANGUAGE")
    yandex_disk_token: str = Field(..., alias="YANDEX_DISK_TOKEN")
    yandex_root_path: str = Field("/База Знаний", alias="YANDEX_ROOT_PATH")
    database_url: str | None = Field(None, alias="DATABASE_URL")
    admin_user_ids: List[int] = Field(default_factory=list, alias="ADMIN_USER_IDS")
    allowed_telegram_user_ids: List[int] = Field(default_factory=list, alias="ALLOWED_TELEGRAM_USER_IDS")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

def load_settings() -> Settings:
    return Settings()
