from __future__ import annotations

import logging
import sqlalchemy
from telegram.ext import Application

from .settings import load_settings

from .db.session import make_session_factory, Base
from .db.repo_dialogs import DialogsRepo
from .db.repo_kb import KBRepo
from .db.repo_dialog_kb import DialogKBRepo

from .clients.openai_client import OpenAIClient
from .clients.yandex_disk_client import YandexDiskClient

from .kb.embedder import Embedder
from .kb.retriever import Retriever

from .services.dialog_service import DialogService
from .services.dialog_kb_service import DialogKBService
from .services.gen_service import GenService
from .services.rag_service import RagService
from .services.voice_service import VoiceService
from .services.image_service import ImageService
from .services.search_service import SearchService
from .services.authz_service import AuthzService

from .handlers import (
    start, help as help_h, dialogs, model, mode, kb, image, voice, text, status, errors
)


log = logging.getLogger(__name__)


def _post_init(app: Application) -> None:
    # место для post_init, если нужно
    return


def _ensure_schema(engine) -> None:
    """
    Railway-friendly schema bootstrap: create tables + minimal ALTERs.
    Без консоли, без alembic — как у вас уже принято.
    """
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        # dialogs.settings (на случай старых БД)
        conn.execute(sqlalchemy.text("ALTER TABLE dialogs ADD COLUMN IF NOT EXISTS settings JSONB"))
        conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS active_dialog_id INTEGER"))

        # kb_documents extensions
        conn.execute(sqlalchemy.text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS resource_id VARCHAR"))
        conn.execute(sqlalchemy.text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS md5 VARCHAR"))
        conn.execute(sqlalchemy.text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS size BIGINT"))
        conn.execute(sqlalchemy.text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS modified_at TIMESTAMP"))
        conn.execute(sqlalchemy.text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))

        conn.execute(sqlalchemy.text("CREATE UNIQUE INDEX IF NOT EXISTS ix_kb_documents_resource_id ON kb_documents (resource_id)"))

        # dialog_kb_documents table
        conn.execute(sqlalchemy.text("""
            CREATE TABLE IF NOT EXISTS dialog_kb_documents (
                id SERIAL PRIMARY KEY,
                dialog_id INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
                is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_dialog_kb_documents_dialog_doc UNIQUE(dialog_id, document_id)
            );
        """))
        conn.execute(sqlalchemy.text("CREATE INDEX IF NOT EXISTS ix_dialog_kb_documents_dialog_id ON dialog_kb_documents (dialog_id)"))
        conn.execute(sqlalchemy.text("CREATE INDEX IF NOT EXISTS ix_dialog_kb_documents_document_id ON dialog_kb_documents (document_id)"))

        # dialog_kb_secrets table
        conn.execute(sqlalchemy.text("""
            CREATE TABLE IF NOT EXISTS dialog_kb_secrets (
                id SERIAL PRIMARY KEY,
                dialog_id INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
                pdf_password TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_dialog_kb_secrets_dialog_doc UNIQUE(dialog_id, document_id)
            );
        """))
        conn.execute(sqlalchemy.text("CREATE INDEX IF NOT EXISTS ix_dialog_kb_secrets_dialog_id ON dialog_kb_secrets (dialog_id)"))


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
    _ensure_schema(engine)

    # --- repos ---
    repo_dialogs = DialogsRepo(session_factory)
    kb_repo = KBRepo(session_factory, dim=3072)
    dialog_kb_repo = DialogKBRepo(session_factory)

    # --- clients ---
    openai = OpenAIClient(api_key=cfg.openai_api_key) if getattr(cfg, "openai_api_key", None) else OpenAIClient()
    yd = YandexDiskClient(token=cfg.yandex_token, root_path=getattr(cfg, "yandex_root_path", ""))

    # --- services ---
    ds = DialogService(repo_dialogs)
    dkb = DialogKBService(dialog_kb_repo, kb_repo)

    embedder = Embedder(openai=openai, model="text-embedding-3-large")
    retriever = Retriever(kb_repo=kb_repo, openai=openai, dim=3072)
    rag = RagService(retriever=retriever, dialog_kb=dkb)

    gen = GenService(openai=openai)
    voice_svc = VoiceService(openai=openai)
    image_svc = ImageService(openai=openai)
    search_svc = SearchService(WebSearchClient())
    authz = AuthzService(repo_dialogs)

    # Note: kb syncer (svc_kb_syncer) подключите здесь, если у вас есть стабильный syncer.
    # Сейчас оставляем только “встроенную” логику. Админские команды в kb.py будут предупреждать, если syncer не настроен.

    # --- bot_data ---
    app.bot_data["settings"] = cfg
    app.bot_data["svc_dialog"] = ds
    app.bot_data["svc_dialog_kb"] = dkb
    app.bot_data["svc_rag"] = rag
    app.bot_data["svc_gen"] = gen
    app.bot_data["svc_voice"] = voice_svc
    app.bot_data["svc_image"] = image_svc
    app.bot_data["svc_search"] = search_svc
    app.bot_data["svc_authz"] = authz

    app.bot_data["repo_dialogs"] = repo_dialogs
    app.bot_data["kb_repo"] = kb_repo
    app.bot_data["openai"] = openai
    app.bot_data["yandex"] = yd
    app.bot_data["retriever"] = retriever
    app.bot_data["embedder"] = embedder

    # --- handlers ---
    errors.register(app)
    start.register(app)
    help_h.register(app)
    dialogs.register(app)
    model.register(app)
    mode.register(app)
    kb.register(app)
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
