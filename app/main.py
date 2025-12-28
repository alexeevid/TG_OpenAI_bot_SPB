from __future__ import annotations

import logging
import sqlalchemy  # 👈 Импортируем весь модуль
from telegram.ext import Application

# Настройки проекта
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

# Репозитории
from .db.session import make_session_factory, init_db
from .db.repo_dialogs import DialogsRepo
from .db.repo_kb import KBRepo

# KB
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
    # dialogs_menu (removed),
)

async def _post_init(app: Application) -> None:
    try:
        await app.bot.delete_my_commands()
        await app.bot.set_my_commands([
            ("start", "Приветствие и инициализация"),
            ("help", "Справка по командам"),
            ("dialogs", "Управление диалогами"),
            ("reset", "Новый диалог"),
            ("status", "Сводка по текущему диалогу"),
            ("model", "Выбрать модель"),
            ("mode", "Выбрать стиль ответа"),
            ("kb", "Поиск по базе знаний"),
            ("update", "Обновить базу знаний"),
            ("img", "Сгенерировать изображение"),
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
        raise RuntimeError("TELEGRAM_TOKEN is not set")

    sf = make_session_factory(cfg.database_url)
    init_db(sf)

    repo_dialogs = DialogsRepo(sf)
    repo_kb = KBRepo(sf)

    oai_client = OpenAIClient(cfg)
    yd = YandexDiskClient(cfg)

    ds = DialogService(repo_dialogs)
    gen = GenService(oai_client, cfg)
    img = ImageService(oai_client, cfg)
    vs = VoiceService(oai_client, cfg)

    embedder = Embedder(oai_client, cfg.openai_embedding_model)
    retriever = Retriever(repo_kb, oai_client, getattr(cfg, "pgvector_dim", 3072))
    rag = RagService(retriever)
    authz = AuthzService(cfg)
    syncer = KBSyncer(yd, embedder, repo_kb, cfg)

    app = Application.builder().token(cfg.telegram_token).post_init(_post_init).build()

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
    dialogs.register(app)
    model.register(app)
    mode.register(app)
    image.register(app)
    voice.register(app)
    text.register(app)
    status.register(app)

    return app

def run() -> None:
    app = build_application()
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=None,
        stop_signals=None,
    )
