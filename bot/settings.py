from pydantic_settings import BaseSettings
from pydantic import Field
from dotenv import load_dotenv
from typing import List

load_dotenv()

class Settings(BaseSettings):
    database_url: str = Field(..., alias="DATABASE_URL")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")
    image_model: str = Field("gpt-image-1", alias="IMAGE_MODEL")
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    allowed_set: List[int] = Field(default_factory=list, alias="ALLOWED_TELEGRAM_USER_IDS")
    admin_set: List[int] = Field(default_factory=list, alias="ADMIN_USER_IDS")
    yandex_disk_token: str = Field("", alias="YANDEX_DISK_TOKEN")
    yandex_root_path: str = Field("/База Знаний", alias="YANDEX_ROOT_PATH")

    class Config:
        populate_by_name = True
        env_file = ".env"
        env_file_encoding = "utf-8"
