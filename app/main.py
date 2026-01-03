# app/main.py
from __future__ import annotations

import logging
import sqlalchemy
from telegram.ext import Application

from .settings import load_settings

from .clients.openai_client import OpenAIClient
from .clients.yandex_disk_client import YandexDiskClient

from .db.session import make_session_factory
from .db.models import Base
from .db.repo_dialogs import DialogsRepo
from .db.repo_kb import KBRepo
from .db.repo_dialog_kb import DialogKBRepo

from .kb.embedder import Embedder
from .kb.retriever import Retriever
from .kb.syncer import KBSyncer

from .services.dialog_service import DialogService
from .services.dialog_kb_service import DialogKBService
from .services.gen_service import GenService
from .services.rag_service import RagService
from .services.voice_service import VoiceService
from .services.image_service import ImageService
from .services.authz_service import AuthzService

from .handlers import start, help, errors, dialogs, model, mode, image, voice, text, status, kb
from .handlers import kb_ui  # NEW


log = logging.getLogger(__name__)


def _post_init(app: Application) -> None:
    try:
        app.bot.set_my_commands([
            ("start", "Старт"),
            ("help", "Помощь"),
            ("dialogs", "Управление диалогами"),
            ("model", "Выбрать модель"),
            ("mode", "Режим ответа"),
            ("kb", "База знаний (диалоговая)"),
            ("status", "Сводка по диалогу"),
        ])
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)


def _ensure_dialog_kb_schema(engine) -> None:
    """
    Railway-friendly bootstrap: создаём нужные таблицы без alembic/консоли.
    """
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("ALTER TABLE dialogs ADD COLUMN IF NOT EXISTS settings JSONB"))
        conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS active_dialog_id INTEGER"))

        conn.execute(sqlalchemy.text("""
            CREATE TABLE IF NOT EXISTS dialog_kb_documents (
                dialog_id INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
                is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_dialog_kb_documents UNIQUE(dialog_id, document_id)
            );
        """))
        conn.execute(sqlalchemy.text("CREATE INDEX IF NOT EXISTS ix_dialog_kb_documents_dialog_id ON dialog_kb_documents (dialog_id)"))
        conn.execute(sqlalchemy.text("CREATE INDEX IF NOT EXISTS ix_dialog_kb_documents_document_id ON dialog_kb_documents (document_id)"))

        conn.execute(sqlalchemy.text("""
            CREATE TABLE IF NOT EXISTS dialog_kb_secrets (
                dialog_id INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
                pdf_password TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_dialog_kb_secrets UNIQUE(dialog_id, document_id)
            );
        """))


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
    _ensure_dialog_kb_schema(engine)

    # --- repos ---
    repo_dialogs = DialogsRepo(session_factory)
    repo_kb = KBRepo(session_factory, getattr(cfg, "pgvector_dim", 3072))
    repo_dialog_kb = DialogKBRepo(session_factory)

    # --- services ---
    ds = DialogService(repo_dialogs)
    authz = AuthzService(cfg)

    oai_client = OpenAIClient(api_key=cfg.openai_api_key)
    gen = GenService(api_key=cfg.openai_api_key, default_model=cfg.text_model)

    img = ImageService(api_key=cfg.openai_api_key, image_model=cfg.image_model) if cfg.enable_image_generation else None
    vs = VoiceService(openai_client=oai_client)

    # KB core
    yd = YandexDiskClient(cfg.yandex_disk_token, cfg.yandex_root_path)
    embedder = Embedder(oai_client, cfg.openai_embedding_model)
    retriever = Retriever(repo_kb, oai_client, getattr(cfg, "pgvector_dim", 3072))

    dialog_kb = DialogKBService(repo_dialog_kb, repo_kb)
    rag = RagService(retriever, dialog_kb)

    # syncer (если нужен админский /kb sync)
    syncer = KBSyncer(yd, embedder, repo_kb, cfg)

    # --- bot_data ---
    app.bot_data.update({
        "settings": cfg,
        "repo_dialogs": repo_dialogs,
        "repo_kb": repo_kb,

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
    })

    # --- handlers ---
    start.register(app)
    help.register(app)
    errors.register(app)

    dialogs.register(app)  # важно до text
    model.register(app)
    mode.register(app)

    kb.register(app)       # NEW
    kb_ui.register(app)    # NEW (callback UI)

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
