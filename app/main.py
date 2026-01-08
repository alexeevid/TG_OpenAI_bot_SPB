from __future__ import annotations

import logging

from telegram.ext import Application

from .settings import load_settings

from .clients.openai_client import OpenAIClient
from .clients.yandex_disk_client import YandexDiskClient
from .clients.web_search_client import WebSearchClient

from .db.session import make_session_factory, reset_schema, ensure_schema
from .db.repo_dialogs import DialogsRepo
from .db.repo_dialog_kb import DialogKBRepo
from .db.repo_kb import KBRepo

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
from .services.search_service import SearchService
from .services.document_service import DocumentService

from .handlers import (
    start,
    help,
    errors,
    dialogs,
    model,
    mode,
    image,
    voice,
    text,
    status,
    kb,
    kb_ui,
    web,
    files,
)

log = logging.getLogger(__name__)

EMBEDDING_DIM = 1536


async def _post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands(
            [
                ("start", "Старт"),
                ("help", "Справка"),
                ("reset", "Сбросить диалог"),
                ("stats", "Статистика"),
                ("kb", "База знаний"),
                ("model", "Модель"),
                ("mode", "Режим ответов"),
                ("dialogs", "Диалоги"),
                ("web", "Веб-поиск"),
            ]
        )
    except Exception:
        log.exception("Failed to set bot commands")


def _setup_logging(cfg) -> None:
    level = getattr(cfg, "log_level", "DEBUG")
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.DEBUG),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("Logging initialized: level=%s", level)


def build_application() -> Application:
    cfg = load_settings()
    _setup_logging(cfg)

    log.info(
        "Startup config: image=%s web=%s provider=%s",
        bool(getattr(cfg, "enable_image_generation", False)),
        bool(getattr(cfg, "enable_web_search", False)),
        getattr(cfg, "web_search_provider", None),
    )

    # --- DB ---
    sf, engine = make_session_factory(cfg.database_url)

    if bool(getattr(cfg, "reset_db", False)):
        reset_schema(engine)

    ensure_schema(engine)

    # --- clients ---
    openai = OpenAIClient(cfg.openai_api_key)
    yandex = YandexDiskClient(cfg.yandex_disk_token, cfg.yandex_root_path)

    web_client = WebSearchClient(
        cfg.web_search_provider,
        tavily_api_key=cfg.tavily_api_key,
        enabled=cfg.enable_web_search,
    )

    # --- repos ---
    repo_dialogs = DialogsRepo(sf)
    repo_kb = KBRepo(sf, dim=EMBEDDING_DIM)
    repo_dialog_kb = DialogKBRepo(sf)

    # --- KB / RAG ---
    embedder = Embedder(cfg, openai)
    retriever = Retriever(cfg, repo_kb, embedder)
    indexer = KbIndexer(repo_kb, embedder, cfg.chunk_size, cfg.chunk_overlap)
    syncer = KBSyncer(cfg, repo_kb, indexer, yandex)

    dialog_service = DialogService(repo_dialogs, settings=cfg)
    dialog_kb_service = DialogKBService(repo_dialog_kb, repo_kb)
    rag_service = RagService(retriever, dialog_kb_service)

    # --- generation ---
    gen_service = GenService(
        api_key=cfg.openai_api_key,
        default_model=cfg.openai_text_model,
        temperature=cfg.openai_temperature,
        max_output_tokens=getattr(cfg, "openai_max_output_tokens", None),
        reasoning_effort=getattr(cfg, "openai_reasoning_effort", None),
        image_model=cfg.openai_image_model,
        transcribe_model=cfg.openai_transcribe_model,
    )

    voice_service = VoiceService(openai, cfg)
    image_service = ImageService(cfg.openai_api_key, cfg.openai_image_model)
    authz_service = AuthzService(cfg)

    search_service = SearchService(web_client, enabled=cfg.enable_web_search)

    # --- documents / OCR ---
    document_service = DocumentService(openai, cfg)

    app = Application.builder().token(cfg.telegram_bot_token).post_init(_post_init).build()

    app.bot_data.update(
        {
            "settings": cfg,
            "openai": openai,
            "yandex": yandex,
            "web_client": web_client,
            "repo_dialogs": repo_dialogs,
            "repo_kb": repo_kb,
            "repo_dialog_kb": repo_dialog_kb,
            "kb_syncer": syncer,
            "svc_syncer": syncer,
            "svc_dialog": dialog_service,
            "svc_dialog_kb": dialog_kb_service,
            "svc_rag": rag_service,
            "svc_gen": gen_service,
            "svc_voice": voice_service,
            "svc_image": image_service,
            "svc_authz": authz_service,
            "svc_search": search_service,
            "svc_document": document_service,
        }
    )

    start.register(app)
    help.register(app)
    dialogs.register(app)
    model.register(app)
    mode.register(app)
    web.register(app)

    # важно: files раньше text (group=9 vs group=10)
    files.register(app)

    image.register(app)
    voice.register(app)
    kb.register(app)
    kb_ui.register(app)
    status.register(app)
    text.register(app)

    errors.register(app)

    return app


def run() -> None:
    app = build_application()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run()
