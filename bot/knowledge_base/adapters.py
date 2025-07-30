# bot/knowledge_base/adapters.py
from __future__ import annotations
from typing import Iterable, List, Sequence

from .interfaces import (
    IKnowledgeBaseIndexer, IKnowledgeBaseRetriever, IContextBuilder,
    KbDocument, KbChunk, KbSyncResult
)

# Пытаемся импортировать существующую реализацию. Если её нет — бросим ImportError вверх.
from bot.knowledge_base.indexer import KnowledgeBaseIndexer as RawIndexer
from bot.knowledge_base.retriever import KnowledgeBaseRetriever as RawRetriever
from bot.knowledge_base.context_manager import ContextManager as RawContext


class IndexerAdapter(IKnowledgeBaseIndexer):
    """
    Обёртка над вашим Indexer: нормализует конструктор и методы
    (sync(), list_documents()/list_docs/list_all/get_documents).
    """
    def __init__(self, settings=None):
        try:
            self._raw = RawIndexer(settings)   # если класс принимает settings
        except TypeError:
            self._raw = RawIndexer()           # если нет

        # Подбираем метод получения списка документов
        if   hasattr(self._raw, "list_documents"):
            self._list = self._raw.list_documents
        elif hasattr(self._raw, "list_docs"):
            self._list = self._raw.list_docs
        elif hasattr(self._raw, "list_all"):
            self._list = self._raw.list_all
        elif hasattr(self._raw, "get_documents"):
            self._list = self._raw.get_documents
        else:
            raise RuntimeError("Indexer has no list_documents/list_docs/list_all/get_documents")

    def sync(self) -> KbSyncResult:
        res = self._raw.sync()
        # Нормализуем результат
        if isinstance(res, tuple) and len(res) == 4:
            return KbSyncResult(*res)
        if isinstance(res, dict):
            return KbSyncResult(
                res.get("added", 0),
                res.get("updated", 0),
                res.get("deleted", 0),
                res.get("unchanged", 0),
            )
        raise RuntimeError("Indexer.sync() returned unexpected result")

    def list_documents(self) -> Iterable[KbDocument]:
        docs = self._list()
        norm = []
        for d in docs:
            # d может быть dataclass/obj/dict — достаём поля максимально безопасно
            id_   = getattr(d, "id",   None) or (d.get("id")    if isinstance(d, dict) else None)
            title = getattr(d, "title",None) or (d.get("title") if isinstance(d, dict) else None)
            path  = getattr(d, "path", None) or (d.get("path")  if isinstance(d, dict) else None)
            mime  = getattr(d, "mime", None) or (d.get("mime")  if isinstance(d, dict) else None)
            size  = getattr(d, "size", None) or (d.get("size")  if isinstance(d, dict) else None)
            sha   = getattr(d, "sha256", None) or (d.get("sha256") if isinstance(d, dict) else None)
            enc   = getattr(d, "encrypted", False) or (d.get("encrypted", False) if isinstance(d, dict) else False)
            if not id_ or not title:
                continue
            norm.append(KbDocument(
                id=str(id_), title=str(title), path=path, mime=mime, size=size, sha256=sha, encrypted=bool(enc)
            ))
        return norm


class RetrieverAdapter(IKnowledgeBaseRetriever):
    def __init__(self, settings=None):
        try:
            self._raw = RawRetriever(settings)
        except TypeError:
            self._raw = RawRetriever()

    def retrieve(self, query: str, doc_ids: Sequence[str], top_k: int = 8) -> List[KbChunk]:
        chunks = self._raw.retrieve(query, list(doc_ids))
        out: List[KbChunk] = []
        for c in chunks:
            text = getattr(c, "text", None) or (c.get("text") if isinstance(c, dict) else None)
            doc  = getattr(c, "doc_id", None) or (c.get("doc_id") if isinstance(c, dict) else None)
            score= getattr(c, "score", None) or (c.get("score")  if isinstance(c, dict) else None)
            page = getattr(c, "page",  None) or (c.get("page")   if isinstance(c, dict) else None)
            if text and doc:
                out.append(KbChunk(doc_id=str(doc), text=str(text), score=score, page=page))
        return out[:top_k]


class ContextAdapter(IContextBuilder):
    def __init__(self, settings=None):
        try:
            self._raw = RawContext(settings)
        except TypeError:
            self._raw = RawContext()

    def build(self, chunks, max_chars: int = 8000) -> str | None:
        texts = [c.text for c in chunks]
        if hasattr(self._raw, "build_context"):
            ctx = self._raw.build_context(texts)
        else:
            ctx = "\n\n".join(texts)
        if not ctx:
            return None
        return ctx[:max_chars]
