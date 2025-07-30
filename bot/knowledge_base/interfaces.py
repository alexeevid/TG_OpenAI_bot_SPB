# bot/knowledge_base/interfaces.py
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class KbDocument:
    id: str
    title: str
    path: Optional[str] = None
    mime: Optional[str] = None
    size: Optional[int] = None
    sha256: Optional[str] = None
    encrypted: bool = False


@dataclass(frozen=True)
class KbChunk:
    doc_id: str
    text: str
    score: Optional[float] = None
    page: Optional[int] = None


@dataclass(frozen=True)
class KbSyncResult:
    added: int
    updated: int
    deleted: int
    unchanged: int


class IKnowledgeBaseIndexer(ABC):
    @abstractmethod
    def sync(self) -> KbSyncResult: ...
    @abstractmethod
    def list_documents(self) -> Iterable[KbDocument]: ...


class IKnowledgeBaseRetriever(ABC):
    @abstractmethod
    def retrieve(self, query: str, doc_ids: Sequence[str], top_k: int = 8) -> List[KbChunk]: ...


class IContextBuilder(ABC):
    @abstractmethod
    def build(self, chunks: Sequence[KbChunk], max_chars: int = 8000) -> Optional[str]: ...
