from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class Transcript:
    text: str
    lang: str | None = None
    duration_sec: float | None = None


@dataclass
class RetrievedChunk:
    id: int
    text: str
    score: float

    document_id: Optional[int] = None
    document_title: Optional[str] = None
    document_path: Optional[str] = None


@dataclass
class ModelAnswer:
    text: str
    citations: list[RetrievedChunk] | None = None
    meta: dict[str, Any] | None = None
