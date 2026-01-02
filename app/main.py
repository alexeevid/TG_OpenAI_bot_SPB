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

from .kb.embedder import Embedder
from .kb.retriever import Retriever
from .kb.syncer import KBSyncer

from .services.dialog_service import DialogService
from .services.rag_service import RagService
from .services.gen_service import GenService
from .services.voice_service import VoiceService
from .services.image_service import ImageService
from .services.authz_service import AuthzService
from .services.search_service import SearchService
from .clients.web_search_client import WebSearchClient

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
    errors,
    kb as kb_handler,
)


async def _post_init(app: Application) -> None:
    try:
        await app.bot.delete_my_commands()
        await app.bot.set_my_commands([
            ("start", "Приветствие и инициализация"),
            ("help", "Справка по командам"),
            ("dialogs", "Управление диалогами"),
            ("model", "Выбрать модель: /model <название>"),
            ("mode", "Режим ответа: concise|detailed|mcwilliams"),
            ("img", "Сгенерировать изображение"),
            ("stats", "Статистика текущего диалога"),
            ("kb", "Поиск/управление базой знаний"),
            ("update", "Обновить (sync) базу знаний"),
            ("status", "Сводка по текущему диалогу"),
        ])
    except Exception as e:
        logging.getLogger(__name__).warning("set_my_commands failed: %s", e)


def _ensure_kb_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        conn.execute(sqlalchemy.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name='kb_chunks' AND column_name='embedding'
                ) THEN
                    BEGIN
                        CREATE INDEX IF NOT EXISTS ix_kb_chunks_embedding_ivfflat
                        ON kb_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
                    EXCEPTION WHEN others THEN
                        NULL;
                    END;
                END IF;
            END $$;
            """
        ))


def build_application() -> Application:
    cfg = load_settings()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not cfg.telegram_token:
        raise RuntimeError("telegram_token отсутствует в настройках")
    if not cfg.database_url:
        raise RuntimeError("DATABASE_URL отсутствует в настройках")

    app = Application.builder().token(cfg.telegram_token).post_init(_post_init).build()

    session_factory, engine = make_session_factory(cfg.database_url)

    _ensure_kb_schema(engine)

    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("ALTER TABLE dialogs ADD COLUMN IF NOT EXISTS settings JSONB"))
        conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS active_dialog_id INTEGER"))

    repo_dialogs = DialogsRepo(session_factory)
    svc_dialog = DialogService(repo_dialogs)

    oai_client = OpenAIClient(api_key=cfg.openai_api_key)
    svc_gen = GenService(api_key=cfg.openai_api_key, default_model=cfg.text_model)
    svc_voice = VoiceService(openai_client=oai_client)
    svc_image = ImageService(api_key=cfg.openai_api_key, image_model=cfg.image_model) if cfg.enable_image_generation else None
    svc_authz = AuthzService(cfg)
    svc_search = SearchService(WebSearchClient(cfg.web_search_provider))

    dim = int(getattr(cfg, "pgvector_dim", 3072))
    kb_repo = KBRepo(session_factory, dim)
    yd = YandexDiskClient(cfg.yandex_disk_token, cfg.yandex_root_path)
    embedder = Embedder(oai_client, cfg.openai_embedding_model)
    retriever = Retriever(kb_repo, embedder, top_k_default=getattr(cfg, "max_kb_chunks", 6))
    svc_rag = RagService(retriever)
    svc_syncer = KBSyncer(yd, embedder, kb_repo, cfg, session_factory)

    app.bot_data.update({
        "settings": cfg,
        "svc_dialog": svc_dialog,
        "svc_gen": svc_gen,
        "svc_image": svc_image,
        "svc_voice": svc_voice,
        "svc_search": svc_search,
        "svc_authz": svc_authz,
        "repo_dialogs": repo_dialogs,
        "repo_kb": kb_repo,
        "svc_rag": svc_rag,
        "yandex": yd,
        "embedder": embedder,
        "svc_syncer": svc_syncer,
    })

    start.register(app)
    help.register(app)
    errors.register(app)
    dialogs.register(app)
    model.register(app)
    mode.register(app)
    kb_handler.register(app)
    image.register(app)
    voice.register(app)
    text.register(app)
    status.register(app)

    return app


def run() -> None:
    app = build_application()
    app.run_polling(drop_pending_updates=True, allowed_updates=None, stop_signals=None)
