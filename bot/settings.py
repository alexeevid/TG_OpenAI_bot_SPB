from typing import List, Optional
from pydantic_settings import BaseSettings
from pydantic import Field, AliasChoices

# ----------------- helpers -----------------
def _split_ints(v: Optional[str]) -> List[int]:
    """
    Парсер CSV/SSV -> List[int], устойчив к мусору.
    Примеры: "1,2,3" / "1; 2 ; 3" -> [1,2,3]
    """
    if not v:
        return []
    parts = [p.strip() for p in v.replace(";", ",").split(",") if p.strip()]
    out: List[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except Exception:
            # игнорируем некорректные элементы
            pass
    return out

def _split_strs(v: Optional[str]) -> List[str]:
    """
    Парсер CSV/SSV или JSON-массива -> List[str].
    Примеры:
      "gpt-4o,gpt-4o-mini" -> ["gpt-4o","gpt-4o-mini"]
      '["gpt-4o","gpt-4o-mini"]' -> ["gpt-4o","gpt-4o-mini"]
    """
    if not v:
        return []
    s = v.strip()
    # пробуем как JSON-массив
    if s.startswith("[") and s.endswith("]"):
        try:
            import json
            data = json.loads(s)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
    # иначе CSV/SSV
    parts = [p.strip() for p in s.replace(";", ",").split(",")]
    return [p for p in parts if p]

# ----------------- settings -----------------
class Settings(BaseSettings):
    # DB: поддерживаем и DATABASE_URL, и POSTGRES_URL
    database_url: str = Field(
        ...,
        validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL"),
    )

    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # OpenAI
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")
    # модель изображений опциональна — если пусто, в коде есть fallback на dall-e-3
    image_model: Optional[str] = Field(None, alias="IMAGE_MODEL")

    # Telegram
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")

    # ---------- ENV (raw-строки) ----------
    admin_user_ids_env: Optional[str] = Field(
        None, validation_alias=AliasChoices("ADMIN_USER_IDS", "ADMIN_SET")
    )
    allowed_user_ids_env: Optional[str] = Field(
        None, validation_alias=AliasChoices("ALLOWED_TELEGRAM_USER_IDS", "ALLOWED_USER_IDS")
    )
    allowed_models_whitelist_env: Optional[str] = Field(None, alias="ALLOWED_MODELS_WHITELIST")
    denylist_models_env: Optional[str] = Field(None, alias="DENYLIST_MODELS")

    # ---------- Распарсенные (храним во внутренних полях; НЕ читаются из ENV напрямую) ----------
    _admin_set: List[int] = Field(default_factory=list)
    _allowed_set: List[int] = Field(default_factory=list)
    _allowed_models_whitelist: List[str] = Field(default_factory=list)
    _denylist_models: List[str] = Field(default_factory=list)

    # Yandex Disk
    yandex_disk_token: str = Field("", alias="YANDEX_DISK_TOKEN")
    yandex_root_path: str = Field("/База Знаний", alias="YANDEX_ROOT_PATH")

    # Фичи
    enable_image_generation: bool = Field(True, alias="ENABLE_IMAGE_GENERATION")

    # ---------- post-init: парсим строки в списки ----------
    def model_post_init(self, __context) -> None:
        self._admin_set = _split_ints(self.admin_user_ids_env)
        self._allowed_set = _split_ints(self.allowed_user_ids_env)
        self._allowed_models_whitelist = _split_strs(self.allowed_models_whitelist_env)
        self._denylist_models = _split_strs(self.denylist_models_env)

    # ---------- публичные свойства для использования в коде ----------
    @property
    def admin_set(self) -> List[int]:
        return list(self._admin_set)

    @property
    def allowed_set(self) -> List[int]:
        # Пустой список трактуем как "доступ всем"
        return list(self._allowed_set)

    @property
    def allowed_models_whitelist(self) -> List[str]:
        return list(self._allowed_models_whitelist)

    @property
    def denylist_models(self) -> List[str]:
        return list(self._denylist_models)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        populate_by_name = True
