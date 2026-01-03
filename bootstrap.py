from __future__ import annotations

import logging
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .settings import Settings
from .clients.openai_client import OpenAIClient
from .clients.yandex_disk_client import YandexDiskClient

from .db.session import make_session_factory
from .db.models import Base
from .db.repo_dialogs import DialogsRepo
from .db.repo_kb import KBRepo
from .db.repo_dialog_kb import DialogKBRepo

from .kb.retriever import Retriever
from .kb.embedder import Embedder
from .kb.syncer import KBSyncer

from .services.voice_service import VoiceService
from .services.gen_service import GenService
from .services.rag_service import RagService
from .services.dialog_service import DialogService
from .services.dialog_kb_service import DialogKBService
from .services.image_service import ImageService
from .services.authz_service import AuthzService

log = logging.getLogger(__name__)


def _ensure_schema(engine: Engine) -> None:
    """Автопочинка схемы БД без Alembic (Railway-friendly)."""
    insp = inspect(engine)

    def _apply(stmts):
        if not stmts:
            return
        with engine.begin() as conn:
            for s in stmts:
                log.info("schema: %s", s)
                conn.execute(text(s))

    # users
    if insp.has_table("users"):
        cols = {c["name"] for c in insp.get_columns("users")}
        stmts = []
        if "tg_id" not in cols:
            stmts += [
                "ALTER TABLE users ADD COLUMN tg_id VARCHAR",
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_tg_id ON users (tg_id)",
            ]
        if "role" not in cols:
            stmts += [
                "ALTER TABLE users ADD COLUMN role VARCHAR",
                "UPDATE users SET role = 'user' WHERE role IS NULL",
            ]
        if "active_dialog_id" not in cols:
            stmts += ["ALTER TABLE users ADD COLUMN active_dialog_id INTEGER"]
        if "created_at" not in cols:
            stmts += ["ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT NOW()"]
        if "updated_at" not in cols:
            stmts += ["ALTER TABLE users ADD COLUMN updated_at TIMESTAMP DEFAULT NOW()"]
        _apply(stmts)

    # dialogs
    if insp.has_table("dialogs"):
        cols = {c["name"] for c in insp.get_columns("dialogs")}
        stmts = []
        if "title" not in cols:
            stmts += ["ALTER TABLE dialogs ADD COLUMN title VARCHAR DEFAULT ''"]
        if "settings" not in cols:
            stmts += ["ALTER TABLE dialogs ADD COLUMN settings JSONB"]
        if "created_at" not in cols:
            stmts += ["ALTER TABLE dialogs ADD COLUMN created_at TIMESTAMP DEFAULT NOW()"]
        if "updated_at" not in cols:
            stmts += ["ALTER TABLE dialogs ADD COLUMN updated_at TIMESTAMP DEFAULT NOW()"]
        _apply(stmts)

    # messages
    if insp.has_table("messages"):
        cols = {c["name"] for c in insp.get_columns("messages")}
        stmts = []
        if "created_at" not in cols:
            stmts += ["ALTER TABLE messages ADD COLUMN created_at TIMESTAMP DEFAULT NOW()"]
        _apply(stmts)

    # dialog-kb mapping tables
    stmts = [
        """
        CREATE TABLE IF NOT EXISTS dialog_kb_documents (
            dialog_id INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_dialog_kb_documents UNIQUE(dialog_id, document_id)
        );
        """,
        "CREATE INDEX IF NOT EXISTS ix_dialog_kb_documents_dialog_id ON dialog_kb_documents (dialog_id)",
        "CREATE INDEX IF NOT EXISTS ix_dialog_kb_documents_document_id ON dialog_kb_documents (document_id)",
        """
        CREATE TABLE IF NOT EXISTS dialog_kb_secrets (
            dialog_id INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
            pdf_password TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_dialog_kb_secrets UNIQUE(dialog_id, document_id)
        );
        """,
    ]
    _apply(stmts)


def build(settings: Settings) -> dict:
    sf, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(bind=engine)
    _ensure_schema(engine)

    repo_dialogs = DialogsRepo(sf)
    repo_kb = KBRepo(sf, dim=settings.pgvector_dim)
    repo_dialog_kb = DialogKBRepo(sf)

    openai = OpenAIClient(settings.openai_api_key)
    yd = YandexDiskClient(settings.yandex_disk_token, settings.yandex_root_path)

    retriever = Retriever(repo_kb, openai, settings.pgvector_dim)
    embedder = Embedder(openai, settings.openai_embedding_model)

    dialog_svc = DialogService(repo_dialogs)
    dialog_kb = DialogKBService(repo_dialog_kb, repo_kb)
    rag = RagService(retriever, dialog_kb)

    gen = GenService(api_key=settings.openai_api_key, default_model=settings.openai_text_model, temperature=settings.openai_temperature)
    voice = VoiceService(openai_client=openai, settings=settings)
    image = ImageService(api_key=settings.openai_api_key, image_model=settings.openai_image_model) if settings.enable_image_generation else None
    syncer = KBSyncer(yd, embedder, repo_kb, settings)

    authz = AuthzService(settings)

    return {
        "settings": settings,
        "repo_dialogs": repo_dialogs,
        "repo_kb": repo_kb,
        "svc_dialog": dialog_svc,
        "svc_dialog_kb": dialog_kb,
        "svc_rag": rag,
        "svc_gen": gen,
        "svc_voice": voice,
        "svc_image": image,
        "svc_syncer": syncer,
        "svc_authz": authz,
        "yandex": yd,
        "embedder": embedder,
    }
