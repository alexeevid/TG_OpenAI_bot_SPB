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

from .handlers import start, help, errors, dialogs, model, mode, image, voice, text, status, kb, admin
from .handlers import kb_ui

log = logging.getLogger(__name__)


async def _post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands([
            ("start", "Старт"),
            ("help", "Помощь"),
            ("dialogs", "Управление диалогами"),
            ("model", "Выбрать модель: /model <название>"),
            ("mode", "Режим ответа: concise|detailed|mcwilliams"),
            ("img", "Сгенерировать изображение"),
            ("stats", "Статистика текущего диалога"),
            ("kb", "База знаний: /kb"),
            ("status", "Сводка по текущему диалогу"),
            ("whoami", "Показать роль/доступ"),
            ("reset", "Сбросить текущий диалог"),
        ])
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)


def _ensure_schema(engine) -> None:
    """
    Railway-friendly bootstrap: если у вас нет консоли/alembic,
    таблицы и недостающие колонки создаём/добавляем на старте.

    Это безопасно: используются только IF NOT EXISTS.
    """
    with engine.begin() as conn:
        # users
        conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_id VARCHAR"))
        conn.execute(sqlalchemy.text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_tg_id ON users (tg_id)"))
        conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR"))
        conn.execute(sqlalchemy.text("UPDATE users SET role='user' WHERE role IS NULL"))
        conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS active_dialog_id INTEGER"))
        conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()"))
        # для совместимости с ветками/патчами, где модель User содержит updated_at
        conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()"))

        # dialogs
        conn.execute(sqlalchemy.text("ALTER TABLE dialogs ADD COLUMN IF NOT EXISTS title VARCHAR DEFAULT ''"))
        conn.execute(sqlalchemy.text("ALTER TABLE dialogs ADD COLUMN IF NOT EXISTS settings JSONB"))
        conn.execute(sqlalchemy.text("ALTER TABLE dialogs ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()"))
        conn.execute(sqlalchemy.text("ALTER TABLE dialogs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()"))

        # messages
        conn.execute(sqlalchemy.text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()"))

        # dialog<->kb mapping (диалоговая БЗ)
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

        # pdf secrets per dialog (forward compat)
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
        raise RuntimeError("TELEGRAM_BOT_TOKEN отсутствует в настройках")

    app = Application.builder().token(cfg.telegram_token).post_init(_post_init).build()

    if not cfg.database_url:
        raise RuntimeError("DATABASE_URL отсутствует в настройках")

    session_factory, engine = make_session_factory(cfg.database_url)
    Base.metadata.create_all(bind=engine)
    _ensure_schema(engine)

    # repos
    repo_dialogs = DialogsRepo(session_factory)
    repo_kb = KBRepo(session_factory, getattr(cfg, "pgvector_dim", 3072))
    repo_dialog_kb = DialogKBRepo(session_factory)

    # services
    ds = DialogService(repo_dialogs)
    authz = AuthzService(cfg)

    oai_client = OpenAIClient(api_key=cfg.openai_api_key)
    gen = GenService(api_key=cfg.openai_api_key, default_model=cfg.text_model)
    img = ImageService(api_key=cfg.openai_api_key, image_model=cfg.image_model) if getattr(cfg, "enable_image_generation", False) else None
    vs = VoiceService(openai_client=oai_client)

    yd = YandexDiskClient(cfg.yandex_disk_token, cfg.yandex_root_path)
    embedder = Embedder(oai_client, cfg.openai_embedding_model)
    retriever = Retriever(repo_kb, oai_client, getattr(cfg, "pgvector_dim", 3072))

    dialog_kb = DialogKBService(repo_dialog_kb, repo_kb)
    rag = RagService(retriever, dialog_kb)

    syncer = KBSyncer(yd, embedder, repo_kb, cfg)

    app.bot_data.update({
        "settings": cfg,
        "repo_dialogs": repo_dialogs,
        "repo_kb": repo_kb,

        "svc_dialog": ds,
        "svc_authz": authz,
        "svc_gen": gen,
        "svc_image": img,
        "svc_voice": vs,

        "svc_dialog_kb": dialog_kb,
        "svc_rag": rag,

        "yandex": yd,
        "embedder": embedder,
        "svc_syncer": syncer,
    })

    # handlers
    start.register(app)
    help.register(app)
    errors.register(app)

    # dialogs BEFORE text
    dialogs.register(app)

    model.register(app)
    mode.register(app)
    image.register(app)
    voice.register(app)
    text.register(app)
    status.register(app)

    # admin/basic
    admin.register(app)

    # KB + UI callbacks
    kb.register(app)
    kb_ui.register(app)

    return app


def run() -> None:
    app = build_application()
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=None,
        stop_signals=None,
    )
