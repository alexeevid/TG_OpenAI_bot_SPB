# app/main.py
from __future__ import annotations

import logging

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
                ("reset", "Сбросить диалог"),
                ("stats", "Статистика"),
                ("kb", "База знаний"),
                ("model", "Модель"),
                ("dialogs", "Диалоги"),
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
    log.info("Logging initialized: level=%s", getattr(logging, str(level).upper(), logging.DEBUG))


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

    # --- DB (ВАЖНО: make_session_factory возвращает (sf, engine)) ---
    sf, engine = make_session_factory(cfg.database_url)

    if bool(getattr(cfg, "reset_db", False)):
        reset_schema(engine)

    ensure_schema(engine)

    # --- repos ---
    repo_dialogs = DialogsRepo(sf)
    repo_kb = KBRepo(sf)
    repo_dialog_kb = DialogKBRepo(sf)

    # --- external clients ---
    openai = OpenAIClient(cfg.openai_api_key)
    yandex = YandexDiskClient(cfg.yandex_disk_token, cfg.yandex_root_path)

    # --- KB/RAG ---
    embedder = Embedder(cfg, openai)
    retriever = Retriever(cfg, repo_kb, embedder)
    indexer = KbIndexer(cfg, repo_kb, embedder)

    # ВАЖНО: фактический контракт KbSyncer — (settings, repo, indexer, yandex_client)
    syncer = KBSyncer(cfg, repo_kb, indexer, yandex)

    dialog_service = DialogService(repo_dialogs, settings=cfg)
    dialog_kb_service = DialogKBService(repo_dialog_kb, repo_kb)
    rag_service = RagService(retriever, dialog_kb_service)

    # FIX: GenService и ImageService принимают не OpenAIClient/RagService,
    # а (api_key, model params). Иначе в OpenAI уходит неверный ключ и/или объект вместо model.
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

    app.bot_data["kb_syncer"] = syncer
    app.bot_data["svc_syncer"] = syncer

    app.bot_data["svc_dialog"] = dialog_service
    app.bot_data["svc_dialog_kb"] = dialog_kb_service
    app.bot_data["svc_rag"] = rag_service
    app.bot_data["svc_gen"] = gen_service
    app.bot_data["svc_voice"] = voice_service
    app.bot_data["svc_image"] = image_service
    app.bot_data["svc_authz"] = authz_service

    # handlers
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

    # error handler
    errors.register(app)

    return app


def run() -> None:
    app = build_application()
    app.run_polling(drop_pending_updates=True)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
