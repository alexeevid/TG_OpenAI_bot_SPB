from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str = Field(..., validation_alias=AliasChoices('TELEGRAM_BOT_TOKEN'))
    openai_api_key: str = Field(..., validation_alias=AliasChoices('OPENAI_API_KEY'))
    database_url: str = Field(..., validation_alias=AliasChoices('DATABASE_URL', 'POSTGRES_URL'))
    yandex_disk_token: str = Field(..., validation_alias=AliasChoices('YANDEX_DISK_TOKEN'))
    yandex_root_path: str = Field(..., validation_alias=AliasChoices('YANDEX_ROOT_PATH'))

    openai_model: str = Field('gpt-4o-mini', validation_alias=AliasChoices('OPENAI_MODEL'))
    temperature: float = Field(0.2, validation_alias=AliasChoices('OPENAI_TEMPERATURE', 'TEMPERATURE'))
    image_model: str = Field('dall-e-3', validation_alias=AliasChoices('OPENAI_IMAGE_MODEL', 'IMAGE_MODEL'))
    embedding_model: str = Field('text-embedding-3-large', validation_alias=AliasChoices('OPENAI_EMBEDDING_MODEL', 'EMBEDDING_MODEL'))

    admin_user_ids: str = Field('', validation_alias=AliasChoices('ADMIN_USER_IDS'))
    allowed_user_ids: str = Field('', validation_alias=AliasChoices('ALLOWED_USER_IDS'))

    chunk_size: int = Field(1200, validation_alias=AliasChoices('CHUNK_SIZE'))
    chunk_overlap: int = Field(200, validation_alias=AliasChoices('CHUNK_OVERLAP'))
    kb_top_k: int = Field(5, validation_alias=AliasChoices('KB_TOP_K', 'MAX_KB_CHUNKS'))
    max_context_tokens: int = Field(6000, validation_alias=AliasChoices('MAX_CONTEXT_TOKENS'))

    log_level: str = Field('INFO', validation_alias=AliasChoices('LOG_LEVEL'))
    rate_limit_per_min: int = Field(20, validation_alias=AliasChoices('RATE_LIMIT_PER_MIN'))

    # --- feature flags ---
    enable_web_search: bool = Field(False, validation_alias=AliasChoices('ENABLE_WEB_SEARCH'))
    web_search_provider: str = Field('auto', validation_alias=AliasChoices('WEB_SEARCH_PROVIDER'))
    enable_image_analysis: bool = Field(False, validation_alias=AliasChoices('ENABLE_IMAGE_ANALYSIS'))

    model_config = SettingsConfigDict(env_prefix='', case_sensitive=True)


def load_settings() -> Settings:
    s = Settings()
    if s.database_url.startswith('postgres://'):
        s.database_url = 'postgresql://' + s.database_url[len('postgres://'):]
    return s


# ✅ Явный экспорт инстанса для импорта в других модулях
settings = load_settings()
