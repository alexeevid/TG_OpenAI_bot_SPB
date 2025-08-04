from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List, Optional


class Settings(BaseSettings):
    # ——————————————————————————————————————————————————————————————
    #  Основные параметры работы бота и доступа к API
    # ——————————————————————————————————————————————————————————————
    database_url: str
    openai_api_key: str
    openai_model: str = "gpt-3.5-turbo"
    image_model: str = "dall-e-2"
    telegram_bot_token: str
    allowed_set: List[int] = Field(default_factory=list)
    admin_set: List[int] = Field(default_factory=list)
    yandex_disk_token: Optional[str] = None
    yandex_root_path: str = "/"

    # ——————————————————————————————————————————————————————————————
    #  Параметры RAG: разбиение документов на чанки
    # ——————————————————————————————————————————————————————————————
    chunk_size: int = 1600        # Размер одного чанка в символах
    chunk_overlap: int = 200      # Перекрытие между соседними чанками в символах
    max_kb_chunks: int = 6        # Максимальное число чанков, передаваемых в prompt

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Глобальный экземпляр настроек
settings = Settings()


def load_settings() -> Settings:
    """Возвращает единственный экземпляр настроек для совместимости с main.py"""
    return settings
