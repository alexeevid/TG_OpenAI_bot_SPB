
from dataclasses import dataclass
from typing import Any

@dataclass
class Transcript:
    text: str
    lang: str|None=None
    duration_sec: float|None=None

@dataclass
class RetrievedChunk:
    id: int
    text: str
    score: float

@dataclass
class ModelAnswer:
    text: str
    citations: list[RetrievedChunk] | None = None
    meta: dict[str, Any] | None = None
