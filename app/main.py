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

from .handlers import start, help, errors, dialogs, model, mode, text, kb, status


log = logging.getLogger(__name__)


def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "DEBUG").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.DEBUG),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # noise reduction
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

    log.info("Logging initialized: level=%s", level)


async def _post_init(app: Application) -> None:
    """
    Optional post-init hook for PTB Application.
    Keep it lightweight; heavy tasks should be in background jobs (job_queue) if enabled.
    """
    cfg = app.bot_data.get("settings")
    if not cfg:
        return

    # Optional KB sync scheduler (if enabled)
    try:
        sync_interval = int(getattr(cfg, "kb_sync_interval", 0) or 0)
    except Exception:
        sync_interval = 0

    if sync_interval > 0:
        try:
            jq = app.job_queue
            if jq is None:
                log.warning(
                    "job_queue is not available (python-telegram-bot[job-queue] not installed). "
                    "KB sync scheduler disabled."
                )
                return

            # Avoid scheduling if no syncer
            syncer = app.bot_data.get("kb_syncer")
            if syncer is None:
                log.warning("KB syncer not found in bot_data. Scheduler disabled.")
                return

            async def _job_cb(ctx):
                try:
                    await syncer.sync()
                except Exception:
                    log.exception("KB sync job failed")

            jq.run_repeating(_job_cb, interval=sync_interval, first=sync_interval)
            log.info("KB sync scheduled: interval=%ss", sync_interval)
        except Exception:
            log.exception("Failed to schedule KB sync")


def build_app() -> Application:
    _configure_logging()

    cfg = load_settings()

    # quick startup config log
    try:
        log.info(
            "Startup config: image=%s web=%s provider=%s",
            getattr(cfg, "enable_image_generation", True),
            getattr(cfg, "enable_web_search", True),
            getattr(cfg, "openai_provider", "auto"),
        )
    except Exception:
        # do not break startup due to logging
        pass

    # DB
    sf = make_session_factory(cfg.database_url)
    ensure_schema(sf)

    repo_dialogs = DialogsRepo(sf)
    repo_kb = KBRepo(sf)
    repo_dialog_kb = DialogKBRepo(sf)

    # External clients
    openai = OpenAIClient(cfg.openai_api_key)
    yandex = YandexDiskClient(cfg.yandex_disk_token, cfg.yandex_root_path)

    # KB/RAG pipeline
    embedder = Embedder(cfg, openai)
    retriever = Retriever(cfg, repo_kb, embedder)
    indexer = KbIndexer(cfg, repo_kb, embedder)
    syncer = KBSyncer(cfg, repo_kb, indexer, yandex)

    # Domain services
    dialog_service = DialogService(repo_dialogs, settings=cfg)
    dialog_kb_service = DialogKBService(repo_dialog_kb, repo_kb)
    rag_service = RagService(retriever, dialog_kb_service)

    # ---------------------------
    # FIX: Correct DI / signatures
    # ---------------------------
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

    image_service = ImageService(
        api_key=cfg.openai_api_key,
        image_model=cfg.openai_image_model,
    )

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

    # Кладём syncer по двум ключам: старый/новый
    app.bot_data["kb_syncer"] = syncer
    app.bot_data["svc_syncer"] = syncer

    # handlers
    start.register(app)
    help.register(app)
    dialogs.register(app)
    model.register(app)
    mode.register(app)
    kb.register(app)
    status.register(app)
    text.register(app)

    # error handler
    errors.register(app)

    log.info("Application built OK")
    return app


def run() -> None:
    app = build_app()
    app.run_polling(allowed_updates=None)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
