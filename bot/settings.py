from pydantic_settings import BaseSettings
from pydantic import Field
from dotenv import load_dotenv
import os

load_dotenv()

class Settings(BaseSettings):
    database_url: str = Field(..., alias="DATABASE_URL")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    openai_api_key: str = Field(..., validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o", validation_alias="OPENAI_MODEL")
    image_model: str | None = Field(default=None, validation_alias="IMAGE_MODEL")
    telegram_bot_token: str = Field(..., validation_alias="TELEGRAM_BOT_TOKEN")
    allowed_telegram_user_ids: list[int] = Field(default_factory=list, validation_alias="ALLOWED_TELEGRAM_USER_IDS")
    admin_user_ids: list[int] = Field(default_factory=list, validation_alias="ADMIN_USER_IDS")
    yandex_disk_token: str | None = Field(default=None, validation_alias="YANDEX_DISK_TOKEN")
    yandex_root_path: str | None = Field(default=None, validation_alias="YANDEX_ROOT_PATH")

    class Config:
        env_file = ".env"
        populate_by_name = True

settings = Settings()
