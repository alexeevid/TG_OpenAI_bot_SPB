# bot/knowledge_base/types.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class KBDocument:
    """
    Карточка документа, отображаемая в UI /kb.
    """
    ext_id: str                  # внешний ID/уникальный путь (например, на Я.Диске)
    title: str                   # название для пользователя
    source_path: Optional[str] = None
    size_bytes: Optional[int] = None
    mtime_ts: Optional[float] = None   # unixtime последнего изменения


@dataclass
class KBChunk:
    """
    Отрывок (чанк) документа, идущий в контекст RAG.
    """
    ext_id: str                  # к какому документу относится (KBDocument.ext_id)
    title: str                   # заголовок документа/части
    content: str                 # текст чанка
    score: float = 0.0           # релевантность (вычисляется retriever'ом)
    page: Optional[int] = None   # страница (если есть)
    source_path: Optional[str] = None  # оригинальный путь/URL (если есть)
