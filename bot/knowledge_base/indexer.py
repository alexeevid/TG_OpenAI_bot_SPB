# bot/knowledge_base/indexer.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from bot.db.session import SessionLocal
from bot.db.models import Document  # у вас уже есть такая модель (таблица documents)

@dataclass
class KBDocMeta:
    id: str
    title: str
    path: Optional[str]
    is_encrypted: bool
    size: int
    updated_at: Optional[datetime]

class KnowledgeBaseIndexer:
    # ... ваша существующая инициализация/методы ...

    def list_documents(self) -> List[KBDocMeta]:
        """
        Возвращает список документов из БД (таблица documents).
        Никаких фильтров не применяет — просто весь каталог.
        """
        with SessionLocal() as s:
            rows = s.query(Document).order_by(Document.title.asc()).all()

            docs: List[KBDocMeta] = []
            for r in rows:
                # максимально «безопасно»: берём поля, если они есть
                doc_id = str(getattr(r, "id"))
                title = (
                    getattr(r, "title", None)
                    or getattr(r, "name", None)
                    or getattr(r, "filename", None)
                    or "Без названия"
                )
                path = getattr(r, "path", None)
                is_encrypted = bool(
                    getattr(r, "is_encrypted", False) or getattr(r, "encrypted", False)
                )
                size = int(
                    getattr(r, "size", 0)
                    or getattr(r, "size_bytes", 0)
                    or 0
                )
                updated_at = getattr(r, "updated_at", None) or getattr(r, "modified_at", None)

                docs.append(
                    KBDocMeta(
                        id=doc_id,
                        title=title,
                        path=path,
                        is_encrypted=is_encrypted,
                        size=size,
                        updated_at=updated_at,
                    )
                )
            return docs

    # ваш метод sync(...) оставьте как есть.
