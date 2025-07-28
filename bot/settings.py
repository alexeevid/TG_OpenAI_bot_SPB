from pydantic_settings import BaseSettings
from pydantic import Field, AliasChoices, field_validator
from typing import List

def _split_ints(v: str | List[int] | None) -> List[int]:
    if v is None:
        return []
    if isinstance(v, list):
        return [int(x) for x in v]
    parts = [p.strip() for p in str(v).replace(";", ",").split(",") if p.strip()]
    out: List[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except Exception:
            pass
    return out

def _split_strs(v: str | List[str] | None) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    parts = [p.strip() for p in str(v).replace(";", ",").split(",")]
    return [p for p in parts if p]

class Settings(BaseSettings):
    # DB: поддерживаем DATABASE_URL и POSTGRES_URL
    database_url: str = Field(
        ...,
        validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL"),
    )

    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # OpenAI
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")

    # Модель изображений может быть пустой (тогда в коде fallback на dall-e-3)
    image_model: str | None = Field(None, alias="IMAGE_MODEL")

    # Telegram
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")

    # ACL: поддерживаем оба названия
    admin_set: List[int] = Field(default_factory=list,
        validation_alias=AliasChoices("ADMIN_USER_IDS", "ADMIN_SET"))
    allowed_set: List[int] = Field(default_factory=list,
        validation_alias=AliasChoices("ALLOWED_TELEGRAM_USER_IDS", "ALLOWED_USER_IDS"))

    # Yandex Disk
    yandex_disk_token: str = Field("", alias="YANDEX_DISK_TOKEN")
    yandex_root_path: str = Field("/База Знаний", alias="YANDEX_ROOT_PATH")

    # Фичи
    enable_image_generation: bool = Field(True, alias="ENABLE_IMAGE_GENERATION")
    allowed_models_whitelist: List[str] = Field(default_factory=list, alias="ALLOWED_MODELS_WHITELIST")
    denylist_models: List[str] = Field(default_factory=list, alias="DENYLIST_MODELS")

    @field_validator("admin_set", mode="before")
    @classmethod
    def _parse_admins(cls, v): return _split_ints(v)

    @field_validator("allowed_set", mode="before")
    @classmethod
    def _parse_allowed(cls, v): return _split_ints(v)

    @field_validator("allowed_models_whitelist", "denylist_models", mode="before")
    @classmethod
    def _parse_model_lists(cls, v): return _split_strs(v)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        populate_by_name = True
