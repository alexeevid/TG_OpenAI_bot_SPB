
from pydantic import BaseModel
from typing import ClassVar
import os

class Settings(BaseModel):
    telegram_token: str
    openai_api_key: str | None = None
    database_url: str | None = None
    webhook_domain: str | None = None
    webhook_secret: str | None = None
    bot_language: str = os.getenv("BOT_LANGUAGE","ru")
    chunk_size: int = int(os.getenv("CHUNK_SIZE","1200"))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP","150"))
    max_context_tokens: int = int(os.getenv("MAX_CONTEXT_TOKENS","6000"))
    max_kb_chunks: int = int(os.getenv("MAX_KB_CHUNKS","8"))
    enable_image_generation: bool = os.getenv("ENABLE_IMAGE_GENERATION","1") == "1"
    enable_web_search: bool = os.getenv("ENABLE_WEB_SEARCH","0") == "1"
    image_model: str = os.getenv("OPENAI_IMAGE_MODEL","gpt-image-1")
    text_model: str = os.getenv("OPENAI_TEXT_MODEL","gpt-4o-mini")
    embedding_model: str = os.getenv("OPENAI_EMBEDDING_MODEL","text-embedding-3-large")
    temperature: float = float(os.getenv("OPENAI_TEMPERATURE","0.7"))
    allowed_user_ids: str | None = os.getenv("ALLOWED_USER_IDS")
    admin_user_ids: str | None = os.getenv("ADMIN_USER_IDS")
    yandex_disk_token: str | None = os.getenv("YANDEX_DISK_TOKEN")
    yandex_root_path: str | None = os.getenv("YANDEX_ROOT_PATH","/kb")
    kb_sync_interval: int = int(os.getenv("KB_SYNC_INTERVAL","3600"))
    kb_sync_entrypoint: str | None = os.getenv("KB_SYNC_ENTRYPOINT")
    rate_limit_per_min: int = int(os.getenv("RATE_LIMIT_PER_MIN","20"))
    web_search_provider: str | None = os.getenv("WEB_SEARCH_PROVIDER")
    transcribe_model: str = os.getenv("OPENAI_TRANSCRIBE_MODEL","whisper-1")
    pgvector_dim: int = int(os.getenv("PGVECTOR_DIM","3072"))
    SHOW_VOICE_TRANSCRIPT: ClassVar[bool] = True
    VOICE_TRANSCRIPT_MAXLEN: ClassVar[int] = 400

def load_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return Settings(
        telegram_token=token,
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        database_url=os.getenv("DATABASE_URL"),
        webhook_domain=os.getenv("WEBHOOK_DOMAIN"),
        webhook_secret=os.getenv("WEBHOOK_SECRET"),
    )
settings = load_settings()  # ← модульная «синглтон»-инстанция
