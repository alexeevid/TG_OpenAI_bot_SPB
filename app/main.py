# app/main.py
from __future__ import annotations

import logging
import os

from telegram.ext import Application

from .settings import load_settings

from .clients.openai_client import OpenAIClient
from .clients.yandex_disk_client import YandexDiskClient

from .db.session import make_session_factory, reset_schema, ensure_schema
from .db.repo_dialogs import DialogsRepo
from .db.repo_kb import KBRepo
from .db.repo_dialog_kb import DialogKBRepo

from .kb.embedder import Embedder
from .kb.retriever import Retriever
from .kb.indexer import KbIndexer
from .kb.syncer import KBSyncer

from .services.dialog_service import DialogService
from .services.dialog_kb_service import DialogKBService
from .services.gen_service import GenService
from .services.rag_service import RagService
from .services.voice_service import VoiceService
from .services.image_service import ImageService
from .services.authz_service import AuthzService

from .handlers import start, help, errors, dialogs, model, mode, image, voice, text, status, kb
from .handlers import kb_ui  # inline UI callbacks

log = logging.getLogger(__name__)


async def _post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands(
            [
                ("start", "Старт"),
                ("help", "Справка"),
                ("reset", "Новый диалог"),
                ("dialogs", "Диалоги (выбор/удаление/имя)"),
                ("status", "Статус текущего диалога"),
                ("stats", "Статус (alias)"),
                ("model", "Выбрать модель"),
                ("mode", "Стиль ответа"),
                ("img", "Сгенерировать изображение"),
                ("kb", "База знаний"),
                ("update", "Синхронизировать БЗ"),
                ("config", "Текущая конфигурация"),
                ("about", "О проекте"),
                ("feedback", "Оставить отзыв"),
            ]
        )
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)


def _setup_logging(cfg) -> None:
    level_name = (getattr(cfg, "log_level", "INFO") or "INFO").upper()
    root_level = getattr(logging, level_name, logging.INFO)

    log_format = os.getenv("LOG_FORMAT") or "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=root_level, format=log_format)

    noisy = [
        "telegram",
        "telegram.ext",
        "httpx",
        "httpcore",
        "httpcore.http11",
        "openai",
        "sqlalchemy",
        "urllib3",
    ]
    for name in noisy:
        logging.getLogger(name).setLevel(logging.WARNING)

    log.info("Logging initialized: level=%s", level_name)


def _resolve_embedding_dim(cfg) -> int:
    dim = getattr(cfg, "embedding_dim", None)
    if isinstance(dim, int) and dim > 0:
        return dim

    dim = getattr(cfg, "pgvector_dim", None)
    if isinstance(dim, int) and dim > 0:
        return dim

    return 3072


def _build_kb_indexer(*, repo_kb: KBRepo, embedder: Embedder, cfg) -> KbIndexer:
    chunk_size = getattr(cfg, "chunk_size", 900)
    overlap = getattr(cfg, "chunk_overlap", 150)
    return KbIndexer(repo_kb, embedder, chunk_size, overlap)


def build_application() -> Application:
    cfg = load_settings()
    _setup_logging(cfg)

    if not cfg.telegram_token:
        raise RuntimeError("telegram_token отсутствует в настройках")
    if not cfg.database_url:
        raise RuntimeError("DATABASE_URL отсутствует в настройках")
    if not cfg.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY отсутствует в настройках")

    log.info(
        "Startup config: image=%s web=%s provider=%s",
        bool(getattr(cfg, "enable_image_generation", False)),
        bool(getattr(cfg, "enable_web_search", False)),
        getattr(cfg, "web_search_provider", None),
    )

    sf, engine = make_session_factory(cfg.database_url)

    if bool(getattr(cfg, "reset_db", False)):
        reset_schema(engine)

    ensure_schema(engine)

    repo_dialogs = DialogsRepo(sf)
    repo_kb = KBRepo(sf, dim=_resolve_embedding_dim(cfg))
    repo_dialog_kb = DialogKBRepo(sf)

    openai = OpenAIClient(cfg.openai_api_key)
    yandex = YandexDiskClient(cfg.yandex_disk_token, cfg.yandex_root_path)

    embedder = Embedder(openai, cfg.openai_embedding_model)
    retriever = Retriever(repo_kb, openai, dim=_resolve_embedding_dim(cfg))
    indexer = _build_kb_indexer(repo_kb=repo_kb, embedder=embedder, cfg=cfg)
    syncer = KBSyncer(yandex, repo_kb, indexer)

    dialog_service = DialogService(repo_dialogs, settings=cfg)
    dialog_kb_service = DialogKBService(repo_dialog_kb, repo_kb)
    rag_service = RagService(retriever, dialog_kb_service)
    gen_service = GenService(openai, rag_service, cfg)
    voice_service = VoiceService(openai, cfg)
    image_service = ImageService(openai, cfg.openai_image_model)
    authz_service = AuthzService(cfg)

    app = Application.builder().token(cfg.telegram_token).post_init(_post_init).build()

    # bot_data (services / repos)
    app.bot_data["settings"] = cfg

    app.bot_data["openai"] = openai
    app.bot_data["yandex"] = yandex

    app.bot_data["repo_dialogs"] = repo_dialogs
    app.bot_data["repo_kb"] = repo_kb
    app.bot_data["repo_dialog_kb"] = repo_dialog_kb

    app.bot_data["svc_dialog"] = dialog_service
    app.bot_data["svc_dialog_kb"] = dialog_kb_service
    app.bot_data["svc_rag"] = rag_service
    app.bot_data["svc_gen"] = gen_service
    app.bot_data["svc_voice"] = voice_service
    app.bot_data["svc_image"] = image_service
    app.bot_data["svc_authz"] = authz_service
    app.bot_data["kb_syncer"] = syncer

    # register handlers
    start.register(app)
    help.register(app)
    dialogs.register(app)
    model.register(app)
    mode.register(app)
    image.register(app)
    voice.register(app)
    kb.register(app)
    kb_ui.register(app)
    status.register(app)
    text.register(app)
    errors.register(app)

    log.info("Application built OK")
    return app


def run() -> None:
    """Entry point for run_local.py and production запусков."""
    app = build_application()
    app.run_polling(drop_pending_updates=True)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
