from __future__ import annotations

import logging
import os
from typing import Optional

import psycopg2
from telegram.ext import Application

# Настройки проекта
from .settings import load_settings

# Сервисы
from .services.gen_service import GenService
from .services.image_service import ImageService
from .services.voice_service import VoiceService
from .services.dialog_service import DialogService

# Клиенты
from .clients.openai_client import OpenAIClient

# Бутстрап БД (опционально)
try:
    from .db.bootstrap import ensure_dialog_settings
except Exception:
    def ensure_dialog_settings(conn):
        # Заглушка, если модуля нет
        pass

# Хендлеры (каждый модуль должен иметь функцию register(app))
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


async def _post_init(app: Application) -> None:
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


def _build_db_connection(database_url: str):
    """
    Единая точка подключения к PostgreSQL (psycopg2-binary).
    """
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    return conn


def build_application() -> Application:
    """
    Создаёт Application, инициализирует сервисы и регистрирует хендлеры.
    """
    cfg = load_settings()

    # Логи
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    log = logging.getLogger(__name__)

    # Telegram Application
    if not getattr(cfg, "TELEGRAM_BOT_TOKEN", None):
        raise RuntimeError("TELEGRAM_BOT_TOKEN отсутствует в настройках")

    app = Application.builder() \
        .token(cfg.TELEGRAM_BOT_TOKEN) \
        .post_init(_post_init) \
        .build()

    # База данных
    if not getattr(cfg, "DATABASE_URL", None):
        raise RuntimeError("DATABASE_URL отсутствует в настройках")
    conn = _build_db_connection(cfg.DATABASE_URL)
    try:
        ensure_dialog_settings(conn)  # добавит dialogs.settings jsonb при необходимости
    except Exception as e:
        log.warning("ensure_dialog_settings skipped/failed: %s", e)

    # OpenAI / клиенты
    if not getattr(cfg, "OPENAI_API_KEY", None):
        log.warning("OPENAI_API_KEY пуст — генерация/транскрибирование не заработают")

    oai_client = OpenAIClient(api_key=cfg.OPENAI_API_KEY)

    # Текстовая генерация (Chat Completions)
    default_model = getattr(cfg, "OPENAI_DEFAULT_MODEL", "gpt-4o-mini")
    gen = GenService(api_key=cfg.OPENAI_API_KEY, default_model=default_model)

    # Картинки
    enable_images = bool(getattr(cfg, "ENABLE_IMAGE_GENERATION", True))
    image_model   = getattr(cfg, "OPENAI_IMAGE_MODEL", "gpt-image-1")
    img = ImageService(api_key=cfg.OPENAI_API_KEY, image_model=image_model) if enable_images else None

    # Диалоги
    ds = DialogService(db=conn)  # подстрой параметры под твой конструктор

    # Голосовой сервис (Whisper через OpenAIClient)
    vs = VoiceService(openai_client=oai_client)  # подстрой под твой конструктор

    # Сохраняем сервисы в bot_data (единая точка доступа в хендлерах)
    app.bot_data.update({
        "db_conn": conn,
        "settings": cfg,

        "svc_dialog": ds,
        "svc_gen": gen,
        "svc_image": img,
        "svc_voice": vs,

        # Под будущие сервисы:
        # "svc_search": ...   # веб-поиск
        # "svc_kb": ...       # RAG
    })

    # Регистрация хендлеров (порядок: команды → голос/текст)
    h_start.register(app)
    h_help.register(app)
    h_dialogs.register(app)  # /dialogs, /dialog
    h_model.register(app)    # /model
    h_mode.register(app)     # /mode
    h_image.register(app)    # /img
    h_voice.register(app)    # voice/audio messages
    h_text.register(app)     # обычный текст (в конце, чтобы не перехватывал команды)

    return app


def run() -> None:
    """
    Точка входа; вызывается из run_local.py и на Railway.
    """
    app = build_application()
    # PTB v20: синхронный run_polling (без asyncio.run)
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=None,
        stop_signals=None,
    )
