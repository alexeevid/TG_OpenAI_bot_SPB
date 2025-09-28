
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

def build(settings: Settings) -> dict:
    sf, engine = make_session_factory(settings.database_url)
    Base.metadata.create_all(bind=engine)

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
