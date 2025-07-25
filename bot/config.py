
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List, Optional


class Settings(BaseSettings):
    # TG
    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")

    # OpenAI
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")
    image_model: str = Field("gpt-image-1", alias="IMAGE_MODEL")

    # Yandex Disk REST
    yandex_disk_token: str = Field(alias="YANDEX_DISK_TOKEN")
    yandex_root_path: str = Field("/", alias="YANDEX_ROOT_PATH")

    # DB
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    # Access
    admin_user_ids: List[int] = Field(default_factory=list, alias="ADMIN_USER_IDS")
    allowed_telegram_user_ids: List[int] = Field(default_factory=list, alias="ALLOWED_TELEGRAM_USER_IDS")

    # Misc
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @property
    def admin_set(self) -> set[int]:
        return {int(x) for x in self.admin_user_ids} if self.admin_user_ids else set()

    @property
    def allowed_set(self) -> set[int]:
        return {int(x) for x in self.allowed_telegram_user_ids} if self.allowed_telegram_user_ids else set()


def load_settings() -> Settings:
    return Settings()
