from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

class Settings(BaseSettings):
    # Telegram / OpenAI
    telegram_bot_token: str = Field(..., env="TELEGRAM_BOT_TOKEN")
    openai_api_key: str = Field(..., env="OPENAI_API_KEY")

    openai_model: str = Field("gpt-4o-mini", env="OPENAI_MODEL")
    vision_model: str = Field("gpt-4o-mini", env="VISION_MODEL")
    image_model: str = Field("dall-e-3", env="IMAGE_MODEL")
    tts_model: str = Field("gpt-4o-mini-tts", env="TTS_MODEL")

    openai_temperature: float = Field(0.3, env="OPENAI_TEMPERATURE")
    max_tokens: int = Field(4096, env="MAX_TOKENS")
    max_history_size: int = Field(30, env="MAX_HISTORY_SIZE")
    vision_max_tokens: int = Field(1024, env="VISION_MAX_TOKENS")
    vision_detail: str = Field("low", env="VISION_DETAIL")
    bot_language: str = Field("ru", env="BOT_LANGUAGE")

    enable_image_generation: bool = Field(True, env="ENABLE_IMAGE_GENERATION")
    enable_tts_generation: bool = Field(False, env="ENABLE_TTS_GENERATION")
    functions_max_consecutive_calls: int = Field(3, env="FUNCTIONS_MAX_CONSECUTIVE_CALLS")

    rag_top_k: int = Field(5, env="RAG_TOP_K")

    allowed_models_whitelist: str = Field("", env="ALLOWED_MODELS_WHITELIST")
    denylist_models: str = Field("", env="DENYLIST_MODELS")

    # access control
    allowed_user_ids: str = Field("", env="ALLOWED_USER_IDS")  # "123,456"
    admin_user_ids: str = Field("", env="ADMIN_USER_IDS")

    # DB
    log_level: str = Field("INFO", env="LOG_LEVEL")
    sentry_dsn: str = Field("", env="SENTRY_DSN")

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

def parse_int_list(value: str) -> List[int]:
    if not value:
        return []
    return [int(x.strip()) for x in value.split(",") if x.strip()]

def load_settings() -> "Settings":
    s = Settings()
    s.allowed_user_ids = parse_int_list(s.allowed_user_ids)
    s.admin_user_ids = parse_int_list(s.admin_user_ids)
    return s
