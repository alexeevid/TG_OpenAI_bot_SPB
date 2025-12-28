from __future__ import annotations

import logging
import sqlalchemy
from telegram.ext import Application

from .settings import load_settings

# Сервисы
from .services.gen_service import GenService
from .services.image_service import ImageService
from .services.voice_service import VoiceService
from .services.dialog_service import DialogService
from .services.rag_service import RagService
from .services.authz_service import AuthzService

# Клиенты
from .clients.openai_client import OpenAIClient
from .clients.yandex_disk_client import YandexDiskClient

# База данных
from .db.session import make_session_factory
from .db.repo_dialogs import DialogsRepo
from .db.repo_kb import KBRepo
from .db.models import Base

# База знаний
from .kb.embedder import Embedder
from .kb.retriever import Retriever
from .kb.syncer import KBSyncer

# Хендлеры
from .handlers import (
    start,
    help,
    voice,
    text,
    image,
    model,
    mode,
    dialogs,
    status,
    # dialogs_menu  # REMOVED
)

async def _post_init(app: Application) -> None:
    """
    Публичные команды бота в Telegram UI.
    По требованию: оставляем только одну команду управления диалогами (/dialogs),
    убираем /menu из списка команд.
    """
    try:
        await app.bot.delete_my_commands()
        await app.bot.set_my_commands([
            ("start", "Приветствие и инициализация"),
            ("help", "Справка по командам"),
            ("reset", "Новый диалог"),
            ("dialogs", "Управление диалогами"),
            ("model", "Выбрать модель"),
            ("mode", "Режим ответа"),
            ("img", "Сгенерировать изображение"),
            ("status", "Сводка по текущему диалогу"),
            ("kb", "Поиск по базе знаний"),
            ("update", "Обновить базу знаний"),
            ("config", "Текущая конфигурация"),
            ("about", "О проекте"),
            ("feedback", "Оставить отзыв"),
        ])
    except Exception as e:
        logging.getLogger(__name__).warning("set_my_commands failed: %s", e)

def build_application() -> Application:
    cfg = load_settings()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if not cfg.telegram_token:
        raise RuntimeError("telegram_token отсутствует в настройках")

    app = Application.builder() \
        .token(cfg.telegram_token) \
        .post_init(_post_init) \
        .build()

    db_url = cfg.database_url
    if not db_url:
        raise RuntimeError("DATABASE_URL отсутствует в настройках")

    session_factory, engine = make_session_factory(db_url)
    Base.metadata.create_all(bind=engine)

    # Совместимость со старой схемой
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("ALTER TABLE dialogs ADD COLUMN IF NOT EXISTS settings JSONB"))
        conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS active_dialog_id INTEGER"))

    repo_dialogs = DialogsRepo(session_factory)
    ds = DialogService(repo_dialogs)

    oai_client = OpenAIClient(api_key=cfg.openai_api_key)
    gen = GenService(api_key=cfg.openai_api_key, default_model=cfg.text_model)

    img = ImageService(api_key=cfg.openai_api_key, image_model=cfg.image_model) if cfg.enable_image_generation else None
    vs = VoiceService(openai_client=oai_client)

    repo_kb = KBRepo(session_factory, getattr(cfg, "pgvector_dim", 3072))
    yd = YandexDiskClient(cfg.yandex_disk_token, cfg.yandex_root_path)
    embedder = Embedder(oai_client, cfg.openai_embedding_model)
    retriever = Retriever(repo_kb, oai_client, getattr(cfg, "pgvector_dim", 3072))
    rag = RagService(retriever)
    authz = AuthzService(cfg)
    syncer = KBSyncer(yd, embedder, repo_kb, cfg)

    app.bot_data.update({
        "settings": cfg,
        "svc_dialog": ds,
        "svc_gen": gen,
        "svc_image": img,
        "svc_voice": vs,
        "repo_dialogs": repo_dialogs,
        "repo_kb": repo_kb,
        "svc_rag": rag,
        "svc_authz": authz,
        "yandex": yd,
        "embedder": embedder,
        "svc_syncer": syncer,
    })

    start.register(app)
    help.register(app)

    # ВАЖНО: dialogs.register(app) должен быть до text.register(app),
    # чтобы ConversationHandler (переименование) отрабатывал корректно.
    dialogs.register(app)

    model.register(app)
    mode.register(app)
    image.register(app)
    voice.register(app)
    text.register(app)
    status.register(app)

    # dialogs_menu.register(app)  # REMOVED

    return app

def run() -> None:
    app = build_application()
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=None,
        stop_signals=None,
    )
