# bot/knowledge_base/service.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

@dataclass
class KBDocument:
    id: int | str
    title: str
    encrypted: bool = False

@dataclass
class KBSyncResult:
    added: int
    updated: int
    deleted: int
    unchanged: int
    added_ids: List[int | str]

class KBService:
    """
    Прослойка-адаптер над вашим indexer/retriever/context_manager.
    Старается работать с разными версиями API, не ломая существующую логику.
    """

    def __init__(self, indexer, retriever, ctx_mgr):
        self.indexer = indexer
        self.retriever = retriever
        self.ctx_mgr = ctx_mgr

    # ---------- СИНХРОНИЗАЦИЯ ----------
    def sync(self) -> KBSyncResult:
        """
        Пытаемся вызвать indexer.sync() и вернуть KBSyncResult.
        Поддерживаем 2 формы ответа:
          * (added, updated, deleted, unchanged)
          * (added, updated, deleted, unchanged, added_ids)
        """
        res = self.indexer.sync()
        if isinstance(res, tuple):
            if len(res) == 4:
                a,u,d,n = res
                added_ids: List[int | str] = []
            elif len(res) >= 5:
                a,u,d,n,added_ids = res[:5]
            else:
                raise RuntimeError("Unsupported sync() result shape")
            return KBSyncResult(a,u,d,n,list(added_ids))
        elif isinstance(res, dict):
            # На случай, если у вас словарь
            return KBSyncResult(
                res.get("added",0), res.get("updated",0),
                res.get("deleted",0), res.get("unchanged",0),
                list(res.get("added_ids",[]) or []),
            )
        else:
            raise RuntimeError("Unsupported sync() return type")

    # ---------- ЛИСТИНГ ДОКУМЕНТОВ ----------
    def list_docs(self, page: int = 1, page_size: int = 10, query: Optional[str] = None) -> Tuple[List[KBDocument], int]:
        """
        Возвращает (docs, total_pages). Пытается использовать разные варианты методов у indexer.
        Документ должен содержать id, title, encrypted.
        """
        total_pages = 1
        docs: List[KBDocument] = []

        # Вариант 1: есть метод list_docs(query, page, page_size) -> (list, total_pages)
        if hasattr(self.indexer, "list_docs"):
            res = self.indexer.list_docs(query=query, page=page, page_size=page_size)
            docs_raw, total_pages = res
            docs = [self._adapt_doc(d) for d in docs_raw]
            return docs, int(total_pages)

        # Вариант 2: есть метод list_all() -> list
        if hasattr(self.indexer, "list_all"):
            all_docs = self.indexer.list_all()
            if query:
                all_docs = [d for d in all_docs if query.lower() in (getattr(d, "title", "") or "").lower()]
            total = len(all_docs)
            total_pages = max(1, (total + page_size - 1) // page_size)
            start = (page - 1) * page_size
            slice_docs = all_docs[start: start + page_size]
            docs = [self._adapt_doc(d) for d in slice_docs]
            return docs, total_pages

        # Вариант 3: есть метод get_documents() -> iterable
        if hasattr(self.indexer, "get_documents"):
            docs_iter = list(self.indexer.get_documents())
            if query:
                docs_iter = [d for d in docs_iter if query.lower() in (getattr(d, "title", "") or "").lower()]
            total = len(docs_iter)
            total_pages = max(1, (total + page_size - 1) // page_size)
            start = (page - 1) * page_size
            slice_docs = docs_iter[start: start + page_size]
            docs = [self._adapt_doc(d) for d in slice_docs]
            return docs, total_pages

        raise RuntimeError("Indexer does not expose list_docs/list_all/get_documents")

    def _adapt_doc(self, raw) -> KBDocument:
        # Поддержка разных представлений документов
        id_ = getattr(raw, "id", None) or getattr(raw, "doc_id", None) or getattr(raw, "pk", None)
        title = getattr(raw, "title", None) or getattr(raw, "name", None) or str(id_)
        encrypted = bool(getattr(raw, "encrypted", False) or getattr(raw, "is_encrypted", False))
        return KBDocument(id=id_, title=title, encrypted=encrypted)

    # ---------- МЕТАДАННЫЕ ----------
    def is_encrypted(self, doc_id: int | str) -> bool:
        # Пробуем достать мету по id
        if hasattr(self.indexer, "get_doc_by_id"):
            meta = self.indexer.get_doc_by_id(doc_id)
        elif hasattr(self.indexer, "doc_by_id"):
            meta = self.indexer.doc_by_id(doc_id)
        else:
            return False
        if meta is None:
            return False
        return bool(getattr(meta, "encrypted", False) or getattr(meta, "is_encrypted", False))

    # ---------- ПАРОЛЬ / ИНДЕКСАЦИЯ ЗАШИФРОВАННЫХ ----------
    def index_encrypted_with_password(self, doc_id: int | str, password: str) -> bool:
        """
        Возвращает True, если пароль подошёл и документ успешно проиндексирован/прочитан.
        """
        # Пытаемся найти метод
        if hasattr(self.indexer, "index_encrypted"):
            return bool(self.indexer.index_encrypted(doc_id, password))
        if hasattr(self.indexer, "unlock_and_index"):
            return bool(self.indexer.unlock_and_index(doc_id, password))
        # Если ничего не нашли — считаем, что пароль не поддерживается
        raise RuntimeError("Indexer does not support encrypted documents")

    # ---------- RAG ----------
    def retrieve(self, text: str, selected_doc_ids: Iterable[int | str], top_k: int = 8):
        """
        Возвращает список чанков (как есть), учитывая выбранные документы.
        Поддерживаем два интерфейса retriever:
          * retrieve(text, doc_ids, top_k)
          * retrieve(text, selected_docs)  # старая версия
        """
        try:
            return self.retriever.retrieve(text, list(selected_doc_ids), top_k=top_k)
        except TypeError:
            # старая сигнатура
            return self.retriever.retrieve(text, list(selected_doc_ids))

    def build_context(self, chunks) -> str:
        return self.ctx_mgr.build_context(chunks)
