# bot/knowledge_base/indexer.py
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

try:
    # Настройки опциональны: если нет — работа не сломается
    from bot.settings import Settings  # type: ignore
except Exception:  # pragma: no cover
    class Settings:  # простая заглушка
        pass

logger = logging.getLogger(__name__)


@dataclass
class KBDocument:
    """
    Унифицированное описание документа в БЗ.
    """
    id: str
    title: str
    path: str
    mime: Optional[str] = None
    size: Optional[int] = None
    encrypted: bool = False
    mtime: Optional[float] = None  # unix timestamp

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class KnowledgeBaseIndexer:
    """
    Базовый интерфейс под телеграм-бот:
      - __init__(settings)
      - sync() -> (added, updated, deleted, unchanged)
      - list_documents() -> List[Dict[str, Any]]
      - get_documents_by_ids(ids) -> List[Dict[str, Any]]

    Сейчас — безопасная заглушка: возвращает пустой каталог, но не падает.
    Позже сюда легко встраивается Я.Диск/ФС/БД.
    """
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self._catalog: Dict[str, KBDocument] = {}  # id -> KBDocument

    # --- API для бота ---

    def sync(self) -> Tuple[int, int, int, int]:
        """
        Скан хранилища и обновление self._catalog.

        Возвращает кортеж (added, updated, deleted, unchanged).
        """
        # TODO: Вставьте реальную синхронизацию (Я.Диск / локальные файлы / БД).
        added = 0
        updated = 0
        deleted = 0
        unchanged = len(self._catalog)
        logger.info(
            "KB sync: added=%s updated=%s deleted=%s unchanged=%s",
            added, updated, deleted, unchanged
        )
        return added, updated, deleted, unchanged

    def list_documents(self) -> List[Dict[str, Any]]:
        """
        Список документов для выбора в UI бота.
        """
        return [doc.to_dict() for doc in self._catalog.values()]

    def get_documents_by_ids(self, ids: List[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for _id in ids:
            doc = self._catalog.get(_id)
            if doc:
                out.append(doc.to_dict())
        return out
