from typing import List, Optional
from pydantic_settings import BaseSettings
from pydantic import Field, AliasChoices

# ----- helpers -----
def _split_ints(v: Optional[str]) -> List[int]:
    """Парсер CSV/SSV -> List[int], игнорирует мусор."""
    if not v:
        return []
    parts = [p.strip() for p in v.replace(";", ",").split(",") if p.strip()]
    out: List[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except Exception:
            # игнорируем некорректные куски
            pass
    return out

def _split_strs(v: Optional[str]) -> List[str]:
    """Парсер CSV/SSV -> List[str]. Если приходит JSON-массив, тоже проглотим."""
    if not v:
        return []
    s = v.strip()
    # попытка понять JSON-список
    if (s.startswith("[") and s.endswith("]")) or (s.startswith('["') and s.endswith('"]')):
        try:
            import json
            data = json.loads(s)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
    # иначе трактуем как CSV/SSV
    parts = [p.strip() for p in s.replace(";", ",").split(",")]
    return [p for p in parts if p]

# ----- settings -----
class Settings(BaseSettings):
    # DB: поддерживаем оба названия
    database_url: str = Field(..., validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL"))

    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # OpenAI
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")
    # модель изображений опциональна — при отсутствии будет fallback в коде на dall-e-3
    image_model: Optional[str] = Field(None, alias="IMAGE_MODEL")

    # Telegram
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")

    # ACL: читаем СЫРЫЕ строки из env (как есть), парсим в model_post_init
    admin_user_ids_raw: Optional[str] = Field(
        None, validation_alias=AliasChoices("ADMIN_USER_IDS", "ADMIN_SET")
    )
    allowed_user_ids_raw: Optional[str] = Field(
        None, validation_alias=AliasChoices("ALLOWED_TELEGRAM_USER_IDS", "ALLOWED_USER_IDS")
    )

    # Модельные списки (whitelist/denylist) — также как строки
    allowed_models_whitelist_raw: Optional[str] = Field(None, alias="ALLOWED_MODELS_WHITELIST")
    denylist_models_raw: Optional[str] = Field(None, alias="DENYLIST_MODELS")

    # Уже распарсенные (готовые к использованию) значения
    admin_set: List[int] = Field(default_factory=list)
    allowed_set: List[int] = Field(default_factory=list)
    allowed_models_whitelist: List[str] = Field(default_factory=list)
    denylist_models: List[str] = Field(default_factory=list)

    # Yandex Disk
    yandex_disk_token: str = Field("", alias="YANDEX_DISK_TOKEN")
    yandex_root_path: str = Field("/База Знаний", alias="YANDEX_ROOT_PATH")

    # Фич-флаги
    enable_image_generation: bool = Field(True, alias="ENABLE_IMAGE_GENERATION")

    # ---- post-init: парсим строки в списки
    def model_post_init(self, __context) -> None:
        self.admin_set = _split_ints(self.admin_user_ids_raw)
        self.allowed_set = _split_ints(self.allowed_user_ids_raw)
        self.allowed_models_whitelist = _split_strs(self.allowed_models_whitelist_raw)
        self.denylist_models = _split_strs(self.denylist_models_raw)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        populate_by_name = True
