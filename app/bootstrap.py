from .settings import Settings
from .clients.openai_client import OpenAIClient
from .clients.yandex_disk_client import YandexDiskClient
from .clients.web_search_client import WebSearchClient
from .kb.retriever import Retriever
from .kb.embedder import Embedder
from .services.voice_service import VoiceService
from .services.gen_service import GenService
from .services.rag_service import RagService
from .services.dialog_service import DialogService
from .services.image_service import ImageService
from .services.search_service import SearchService
from .services.authz_service import AuthzService
from .db.session import make_session_factory
from .db.repo_dialogs import DialogsRepo
from .db.repo_kb import KBRepo
from .db.models import Base

from sqlalchemy import inspect, text
import logging
log = logging.getLogger(__name__)

# ⬇️ добавьте импорт
from sqlalchemy import inspect, text

def _ensure_schema(engine):
    """
    Горячая автопочинка схемы в проде:
    users:    tg_id (unique), role (default 'user'), created_at (default NOW())
    dialogs:  user_id, title (default ''), created_at (default NOW())
    messages: dialog_id, role, content, created_at (default NOW())
    + снятие NOT NULL c 'лишних' колонок без дефолта (на случай артефактов старых схем)
    """
    insp = inspect(engine)

    def _apply(stmts):
        if not stmts: return
        with engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))

    # ---- users ----
    if insp.has_table("users"):
        cols = {c["name"] for c in insp.get_columns("users")}
        stmts = []
        if "tg_id" not in cols:
            stmts.append("ALTER TABLE users ADD COLUMN tg_id VARCHAR")
            stmts.append("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_tg_id ON users (tg_id)")
        if "role" not in cols:
            stmts.append("ALTER TABLE users ADD COLUMN role VARCHAR")
            stmts.append("UPDATE users SET role = 'user' WHERE role IS NULL")
        if "created_at" not in cols:
            stmts.append("ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT NOW()")
        _apply(stmts)

    # ---- dialogs ----
    if insp.has_table("dialogs"):
        cols = insp.get_columns("dialogs")
        colnames = {c["name"] for c in cols}
        stmts = []
        if "user_id" not in colnames:
            stmts.append("ALTER TABLE dialogs ADD COLUMN user_id INTEGER")
        if "title" not in colnames:
            stmts.append("ALTER TABLE dialogs ADD COLUMN title VARCHAR")
            stmts.append("UPDATE dialogs SET title = '' WHERE title IS NULL")
        if "created_at" not in colnames:
            stmts.append("ALTER TABLE dialogs ADD COLUMN created_at TIMESTAMP DEFAULT NOW()")
        _apply(stmts)

        # Снимаем NOT NULL у неизвестных полей без дефолта
        keep = {"id","user_id","title","created_at"}
        with engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT column_name, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='dialogs'
            """)).fetchall()
            for name, is_nullable, default in rows:
                if name not in keep and is_nullable == 'NO' and default is None:
                    conn.execute(text(f'ALTER TABLE dialogs ALTER COLUMN "{name}" DROP NOT NULL'))

    # ---- messages ----
    if insp.has_table("messages"):
        cols = insp.get_columns("messages")
        colnames = {c["name"] for c in cols}
        stmts = []
        if "dialog_id" not in colnames:
            stmts.append("ALTER TABLE messages ADD COLUMN dialog_id INTEGER")
        if "role" not in colnames:
            stmts.append("ALTER TABLE messages ADD COLUMN role VARCHAR")
        if "content" not in colnames:
            stmts.append("ALTER TABLE messages ADD COLUMN content TEXT")
        if "created_at" not in colnames:
            stmts.append("ALTER TABLE messages ADD COLUMN created_at TIMESTAMP DEFAULT NOW()")
        _apply(stmts)

        keep = {"id","dialog_id","role","content","created_at"}
        with engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT column_name, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='messages'
            """)).fetchall()
            for name, is_nullable, default in rows:
                if name not in keep and is_nullable == 'NO' and default is None:
                    conn.execute(text(f'ALTER TABLE messages ALTER COLUMN "{name}" DROP NOT NULL'))

def build(settings: Settings) -> dict:
    sf, engine = make_session_factory(settings.database_url)
    # safety net: создаём таблицы, если их нет
    Base.metadata.create_all(bind=engine)
    
    def _dump_table_schema(engine, table):
        insp = inspect(engine)
        if not insp.has_table(table):
            log.info("schema: table %s — нет", table)
            return
        cols = insp.get_columns(table)
        log.info("schema: %s -> %s", table, [(c['name'], c.get('type'), c.get('nullable'), c.get('default')) for c in cols])
    
    _dump_table_schema(engine, "users")
    _dump_table_schema(engine, "dialogs")
    _dump_table_schema(engine, "messages")

    # ⬇️ ВАЖНО: автопроверка/починка схемы
    _ensure_schema(engine)

    repo_dialogs = DialogsRepo(sf)
    kb_repo = KBRepo(sf, dim=settings.pgvector_dim)

    openai = OpenAIClient(settings.openai_api_key)
    yd = YandexDiskClient(settings.yandex_disk_token, settings.yandex_root_path)

    retriever = Retriever(kb_repo, openai, settings.pgvector_dim)
    embedder = Embedder(openai, settings.embedding_model)

    rag = RagService(retriever)
    gen = GenService(openai, rag, settings)
    voice = VoiceService(openai, settings)
    image = ImageService(openai, settings.image_model)
    search = SearchService(WebSearchClient(settings.web_search_provider))
    dialog = DialogService(repo_dialogs)
    authz = AuthzService(settings)

    return {
        "svc_gen": gen,
        "svc_voice": voice,
        "svc_image": image,
        "svc_search": search,
        "svc_dialog": dialog,
        "svc_authz": authz,
        "repo_dialogs": repo_dialogs,
        "kb_repo": kb_repo,
        "openai": openai,
        "yandex": yd,
        "retriever": retriever,
        "embedder": embedder,
    }
