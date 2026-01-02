from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

from app.settings import settings
from app.db.repo_kb import KBRepo


@dataclass
class Chunk:
    text: str
    order: int


def split_text(text: str, chunk_size: int, overlap: int) -> List[Chunk]:
    text = (text or "").strip()
    if not text:
        return []

    out: List[Chunk] = []
    i = 0
    order = 0
    n = len(text)
    step = max(1, chunk_size - overlap)

    while i < n:
        part = text[i : i + chunk_size]
        if part.strip():
            out.append(Chunk(text=part, order=order))
            order += 1
        i += step
    return out


class KbIndexer:
    """
    Делает:
    - delete embeddings by resource_id
    - parse -> split -> embeddings -> bulk insert
    """

    def __init__(self, db, openai_client):
        self._db = db
        self._repo = KBRepo(db)
        self._openai = openai_client

    def delete_file_embeddings(self, resource_id: str) -> None:
        self._repo.delete_chunks_by_resource_id(resource_id)

    def index_document_text(self, resource_id: str, path: str, text: str) -> int:
        doc_id = self._repo.upsert_document(resource_id=resource_id, path=path)
        self._repo.delete_chunks_by_document_id(doc_id)

        chunks = split_text(text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
        if not chunks:
            return 0

        texts = [c.text for c in chunks]

        # embeddings (text-embedding-3-large default, dim 3072) :contentReference[oaicite:5]{index=5}
        vectors = self._openai.embed(texts)

        # bulk insert
        rows = [(doc_id, c.order, c.text, vectors[idx]) for idx, c in enumerate(chunks)]
        self._repo.insert_chunks_bulk(rows)
        return len(rows)

    def extract_text_from_image_via_openai(self, image_bytes: bytes) -> str:
        """
        Опциональная “best” функция (если вы включите): просим модель извлечь текст/описание.
        Реализация зависит от вашего OpenAIClient и того, используете ли Responses API. :contentReference[oaicite:6]{index=6}
        """
        return self._openai.vision_extract_text(image_bytes)

    def index_image(self, resource_id: str, path: str, image_bytes: bytes) -> int:
        if not settings.KB_ENABLE_OPENAI_VISION:
            # fallback: метаданные/placeholder (не OCR)
            text = "[IMAGE] (vision disabled)"
        else:
            text = self.extract_text_from_image_via_openai(image_bytes)

        return self.index_document_text(resource_id, path, text)
