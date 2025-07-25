from pydantic_settings import BaseSettings
from dotenv import load_dotenv
import os

load_dotenv()

class Settings(BaseSettings):
    database_url: str
    log_level: str = "INFO"
    openai_api_key: str
    openai_model: str = "gpt-4o"
    image_model: str | None = None
    telegram_bot_token: str
    allowed_set: str | None = None
    admin_set: str | None = None
    yandex_disk_token: str | None = None
    yandex_root_path: str | None = None

    class Config:
        env_file = ".env"

settings = Settings()
