from __future__ import annotations

import logging
from telegram.ext import Application
from sqlalchemy import text

# Настройки проекта
from .settings import load_settings

# Сервисы
from .services.gen_service import GenService
from .services.image_service import ImageService
from .services.voice_service import VoiceService
from .services.dialog_service import DialogService

# Клиенты
from .clients.openai_client import OpenAIClient

# База данных (SQLAlchemy)
from .db.session import make_session_factory
from .db.repo_dialogs import DialogsRepo
from .db.models import Base

# Хендлеры
from .handlers import (
    start as h_start,
    help as h_help,
    voice as h_voice,
    text as h_text,
    image as h_image,
    model as h_model,
    mode as h_mode,
    dialogs as h_dialogs
)


async def _post_init(app: Application) -> None:
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


def build_application() -> Application:
    cfg = load_settings()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    log = logging.getLogger(__name__)

    # Telegram
    if not getattr(cfg, "TELEGRAM_BOT_TOKEN", None):
        raise RuntimeError("TELEGRAM_BOT_TOKEN отсутствует в настройках")

    app = Application.builder() \
        .token(cfg.TELEGRAM_BOT_TOKEN) \
        .post_init(_post_init) \
        .build()

    # База данных (через SQLAlchemy)
    db_url = cfg.DATABASE_URL
    if not db_url:
        raise RuntimeError("DATABASE_URL отсутствует в настройках/окружении")

    session_factory, engine = make_session_factory(db_url)
    Base.metadata.create_all(bind=engine)

    # Добавим поле settings (если его нет)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE dialogs ADD COLUMN IF NOT EXISTS settings JSONB"))

    repo_dialogs = DialogsRepo(session_factory)

    # OpenAI
    if not getattr(cfg, "OPENAI_API_KEY", None):
        log.warning("OPENAI_API_KEY пуст — генерация/транскрибирование не заработают")

    oai_client = OpenAIClient(api_key=cfg.OPENAI_API_KEY)
    default_model = getattr(cfg, "OPENAI_DEFAULT_MODEL", "gpt-4o-mini")
    gen = GenService(api_key=cfg.OPENAI_API_KEY, default_model=default_model)

    enable_images = bool(getattr(cfg, "ENABLE_IMAGE_GENERATION", True))
    image_model   = getattr(cfg, "OPENAI_IMAGE_MODEL", "gpt-image-1")
    img = ImageService(api_key=cfg.OPENAI_API_KEY, image_model=image_model) if enable_images else None

    ds = DialogService(repo_dialogs)
    vs = VoiceService(openai_client=oai_client)

    app.bot_data.update({
        "settings": cfg,

        "svc_dialog": ds,
        "svc_gen": gen,
        "svc_image": img,
        "svc_voice": vs,

        "repo_dialogs": repo_dialogs,
    })

    # Регистрация хендлеров
    h_start.register(app)
    h_help.register(app)
    h_dialogs.register(app)
    h_model.register(app)
    h_mode.register(app)
    h_image.register(app)
    h_voice.register(app)
    h_text.register(app)

    return app


def run() -> None:
    app = build_application()
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=None,
        stop_signals=None,
    )
