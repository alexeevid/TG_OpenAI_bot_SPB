# bot/config.py
from __future__ import annotations
from bot.settings import Settings


def load_settings() -> Settings:
    return Settings()  # Pydantic Settings сам подтянет переменные окружения
