from pydantic import BaseSettings, Field
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
    # Размер одного чанка в символах
    chunk_size: int = 1600
    # Перекрытие между последовательными чанками (в символах)
    chunk_overlap: int = 200
    # Максимальное число чанков, передаваемых в prompt
    max_kb_chunks: int = 6

    class Config:
        # Файл с переменными окружения
        env_file = ".env"
        env_file_encoding = "utf-8"


# Инициализация глобального объекта настроек
settings = Settings()
