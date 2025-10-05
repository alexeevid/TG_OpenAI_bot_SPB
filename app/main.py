from __future__ import annotations

import logging
import os
import psycopg2
from typing import Optional

from telegram.ext import Application

# 1) Настройки
# Важно: у тебя уже есть Settings + load_settings по трейсам.
# Оставляем этот импорт как в твоём проекте:
from .settings import load_settings

# 2) Сервисы
from .services.gen_service import GenService
from .services.image_service import ImageService
from .services.voice_service import VoiceService
from .services.dialog_service import DialogService

# 3) Клиенты
from .clients.openai_client import OpenAIClient

# 4) Бутстрап БД (опционально: добавляет dialogs.settings jsonb)
# Если такого модуля пока нет — временно закомментируй import/вызов ensure_dialog_settings
try:
    from .db.bootstrap import ensure_dialog_settings
except Exception:
    def ensure_dialog_settings(conn):  # заглушка, если модуля нет
        pass

# 5) Хендлеры (каждый модуль должен иметь функцию register(app))
# Подстрой имена под свою фактическую структуру:
from .handlers import (
    start as h_start,
    help as h_help,
    voice as h_voice,
    text as h_text,
    image as h_image,     # /img
    model as h_model,     # /model
    mode as h_mode,       # /mode
    dialogs as h_dialogs  # /dialogs, /dialog
)

# ---------------------------------------------------------------
# Пост-инициализация (меню команд)
# ---------------------------------------------------------------

async def _post_init(app: Application):
    """
    Стартовый хук: задаём меню /команд в Telegram.
    Вызывается автоматически при запуске run_polling().
    """
    try:
        await app.bot.set_my_commands([
            ("start",  "Приветствие и инициализация"),
            ("help",   "Справка"),
            ("reset",  "Новый диалог"),
            ("dialogs","Список диалогов"),
            ("dialog", "Переключить диалог: /dialog <id>"),
            ("model",  "Модель для текущего диалога"),
            ("mode",   "Режим ответа: concise|detailed|mcwilliams"),
            ("img",    "Сгенерировать изображение"),
            ("stats",  "Статистика бота"),
            ("kb",     "База знаний"),
        ])
    except Exception as e:
        logging.getLogger(__name__).warning("set_my_commands failed: %s", e)

# ---------------------------------------------------------------
# Сборка Application + инициализация сервисов
# ---------------------------------------------------------------

def _build_db_connection(database_url: str):
    """
    Единая точка подключения к PostgreSQL (psycopg2-binary).
    """
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    return conn

def build_application():
    """
    Создаёт Application,
