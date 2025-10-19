from __future__ import annotations

import logging
import os
from typing import Optional

import psycopg2
from telegram.ext import Application

# Настройки проекта
from .settings import load_settings

# Сервисы
from .db.sqlalchemy_factory import make_session_factory
from .db.repo_dialogs import DialogsRepo
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

    # --- helpers ------------------------------------------------------------
    def pick(*names: str, default: Optional[str] = None) -> Optional[str]:
        """
        Берём значение из ENV по любому из имён, иначе из cfg (атрибуты с теми же именами),
        иначе default. Пустые строки считаем отсутствием значения.
        """
        for n in names:
            v = os.getenv(n)
            if v is not None and str(v).strip() != "":
                return v
            if hasattr(cfg, n):
                v = getattr(cfg, n)
                if v is not None and str(v).strip() != "":
                    return v
        return default

    def as_bool(val: Optional[object], default: bool = True) -> bool:
        """Корректно парсим булевы ENV ('1','true','yes','on' => True; '0','false','no','off' => False)."""
        if isinstance(val, bool):
            return val
        if val is None:
            return default
        s = str(val).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        return default
    # -----------------------------------------------------------------------

    # Telegram Application
    tg_token = pick("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_TOKEN")
    if not tg_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN отсутствует в настройках/окружении")

    app = Application.builder().token(tg_token).post_init(_post_init).build()

    # База данных
    db_url = pick("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL отсутствует в настройках/окружении")
    conn = _build_db_connection(db_url)
    session_factory = make_session_factory(db_url)
    repo_dialogs = DialogsRepo(session_factory)

    try:
        ensure_dialog_settings(conn)  # добавит dialogs.settings jsonb при необходимости
    except Exception as e:
        log.warning("ensure_dialog_settings skipped/failed: %s", e)

    # OpenAI / клиенты
    oai_key = pick("OPENAI_API_KEY")
    if not oai_key:
        log.warning("OPENAI_API_KEY пуст — генерация/транскрибирование не заработают")

    oai_client = OpenAIClient(api_key=oai_key)

    # Текстовая генерация (Chat Completions)
    default_model = pick("OPENAI_DEFAULT_MODEL", default="gpt-4o-mini")
    gen = GenService(api_key=oai_key, default_model=default_model)

    # Картинки
    enable_images = as_bool(pick("ENABLE_IMAGE_GENERATION"), default=True)
    image_model   = pick("OPENAI_IMAGE_MODEL", "IMAGE_MODEL", default="gpt-image-1")
    img = ImageService(api_key=oai_key, image_model=image_model) if enable_images else None

    # Диалоги
    repo_dialogs = DialogsRepo(conn)
    ds = DialogService(repo_dialogs)

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
