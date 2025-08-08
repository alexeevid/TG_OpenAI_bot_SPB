from __future__ import annotations
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseModel):
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")
    image_model: str = Field("dall-e-3", alias="OPENAI_IMAGE_MODEL")
    embedding_model: str = Field("text-embedding-3-large", alias="OPENAI_EMBEDDING_MODEL")

    database_url: str = Field(..., alias="DATABASE_URL")

    yandex_disk_token: str = Field(..., alias="YANDEX_DISK_TOKEN")
    yandex_root_path: str = Field(..., alias="YANDEX_ROOT_PATH")

    admin_user_ids: str = Field("", alias="ADMIN_USER_IDS")
    allowed_user_ids: str = Field("", alias="ALLOWED_USER_IDS")

    chunk_size: int = Field(1200, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(200, alias="CHUNK_OVERLAP")
    kb_top_k: int = Field(5, alias="KB_TOP_K")
    max_context_tokens: int = Field(6000, alias="MAX_CONTEXT_TOKENS")

    log_level: str = Field("INFO", alias="LOG_LEVEL")
    rate_limit_per_min: int = Field(20, alias="RATE_LIMIT_PER_MIN")

def load_settings() -> "Settings":
    return Settings()
