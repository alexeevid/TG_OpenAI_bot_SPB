# app/main.py
from __future__ import annotations

import logging
import os

from telegram.ext import Application

from .settings import load_settings

from .clients.openai_client import OpenAIClient
from .clients.yandex_disk_client import YandexDiskClient

from .db.session import make_session_factory, reset_schema, ensure_schema
session_factory, engine = make_session_factory(cfg.database_url)

# Одноразовый reset по env (удобно для Railway, где нет SQL-консоли)
if os.getenv("DB_RESET_ON_START", "").strip() in ("1", "true", "TRUE", "yes", "YES"):
    reset_schema(engine)
else:
    ensure_schema(engine)

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
    """PTB ожидает coroutine. Здесь обязателен async + await."""
    try:
        await app.bot.set_my_commands(
            [
                ("start", "Старт"),
                ("help", "Помощь"),
                ("dialogs", "Управление диалогами"),
                ("model", "Выбрать модель"),
                ("mode", "Режим ответа"),
                ("kb", "База знаний (диалоговая)"),
                ("status", "Сводка по диалогу"),
            ]
        )
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)


def _setup_logging(cfg) -> None:
    """
    Управляемое логирование:
    - общий уровень берём из cfg.log_level
    - шумные либы приглушаем до WARNING
    - формат можно переопределить через env LOG_FORMAT
    """
    level_name = (getattr(cfg, "log_level", "INFO") or "INFO").upper()
    root_level = getattr(logging, level_name, logging.INFO)

    log_format = os.getenv("LOG_FORMAT") or "%(asctime)s %(levelname)s %(name)s: %(message)s"

    logging.basicConfig(
        level=root_level,
        format=log_format,
    )

    # Снижаем шум, чтобы DEBUG был полезен
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
    """
    Единая точка определения размерности embeddings.

    Приоритет:
    1) cfg.embedding_dim (property в Settings)
    2) cfg.pgvector_dim (если где-то используется)
    3) дефолт 3072 (text-embedding-3-large)
    """
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

    return KbIndexer(
        repo_kb,
        embedder,
        chunk_size,
        overlap,
    )


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
        getattr(cfg, "web_search_provider", "disabled"),
    )

    app = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(_post_init)
        .build()
    )

    session_factory, _engine = make_session_factory(cfg.database_url)
    # Schema is managed by Alembic (see Docker CMD).

    # --- repos ---
    repo_dialogs = DialogsRepo(session_factory)

    embedding_dim = _resolve_embedding_dim(cfg)
    repo_kb = KBRepo(session_factory, embedding_dim)

    repo_dialog_kb = DialogKBRepo(session_factory)

    # --- services ---
    ds = DialogService(repo_dialogs)
    authz = AuthzService(cfg)

    oai_client = OpenAIClient(api_key=cfg.openai_api_key)
    gen = GenService(api_key=cfg.openai_api_key, default_model=cfg.text_model)

    img = (
        ImageService(api_key=cfg.openai_api_key, image_model=cfg.image_model)
        if getattr(cfg, "enable_image_generation", False)
        else None
    )
    vs = VoiceService(openai_client=oai_client)

    # KB core
    yd = YandexDiskClient(cfg.yandex_disk_token, cfg.yandex_root_path)

    embedder = Embedder(oai_client, cfg.openai_embedding_model)
    retriever = Retriever(repo_kb, oai_client, embedding_dim)

    dialog_kb = DialogKBService(repo_dialog_kb, repo_kb)
    rag = RagService(retriever, dialog_kb)

    # indexer + syncer (админские /kb sync/scan/status)
    indexer = _build_kb_indexer(repo_kb=repo_kb, embedder=embedder, cfg=cfg)
    syncer = KBSyncer(cfg, repo_kb, indexer, yd)

    # --- bot_data ---
    app.bot_data.update(
        {
            "settings": cfg,
            "repo_dialogs": repo_dialogs,
            "repo_kb": repo_kb,
            "repo_dialog_kb": repo_dialog_kb,
            "oai_client": oai_client,
            "retriever": retriever,
            "indexer": indexer,
            "svc_dialog": ds,
            "svc_authz": authz,
            "svc_gen": gen,
            "svc_voice": vs,
            "svc_image": img,
            "svc_dialog_kb": dialog_kb,
            "svc_rag": rag,
            "yandex": yd,
            "embedder": embedder,
            "svc_syncer": syncer,
        }
    )

    # --- handlers ---
    start.register(app)
    help.register(app)
    errors.register(app)

    dialogs.register(app)
    model.register(app)
    mode.register(app)

    kb.register(app)
    kb_ui.register(app)

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
    )
