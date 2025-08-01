# bot/knowledge_base/retriever.py
from __future__ import annotations

import io
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import yadisk  # type: ignore
except Exception:
    yadisk = None

try:
    from pypdf import PdfReader  # type: ignore
except Exception:
    PdfReader = None  # покажем в логах и соберем пустой контекст

try:
    import numpy as np  # type: ignore
except Exception:
    np = None  # без numpy ранжирование по косинусу не сработает (соберем плоский контекст)

from openai import OpenAI

logger = logging.getLogger(__name__)

# Локальная папка кэша: текст, чанки, эмбеддинги
CACHE_DIR = Path("./kb_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
MANIFEST_PATH = CACHE_DIR / "manifest.json"


@dataclass
class KBChunk:
    path: str          # полный путь на диске (disk:/...)
    chunk_id: int      # порядковый номер
    text: str          # текст чанка
    score: float = 0.0 # релевантность (косинус)


class ContextManager:
    """
    Совместимость с прежним импортом.
    Собирает финальный текст контекста из чанков.
    """

    def __init__(self, settings=None):
        self.max_chars = int(os.getenv("KB_CTX_MAX_CHARS", "6000"))

    def build_context(self, chunks: List[KBChunk]) -> str:
        if not chunks:
            return ""
        # Склеиваем, пока не превысили лимит символов
        parts: List[str] = []
        total = 0
        for ch in chunks:
            piece = f"[{ch.path} :: {ch.chunk_id} :: score={ch.score:.3f}]\n{ch.text.strip()}\n"
            if total + len(piece) > self.max_chars:
                break
            parts.append(piece)
            total += len(piece)
        return "\n".join(parts)


class KnowledgeBaseRetriever:
    """
    Минимальная реализация RAG:
    1) Скачиваем PDF с Я.Диска;
    2) Извлекаем плоский текст;
    3) Режем на чанки и кэшируем (text + embeddings);
    4) По вопросу строим эмбеддинг и ранжируем чанки по косинусу;
    5) Возвращаем top-N.
    """

    def __init__(self, settings):
        self.settings = settings
        self.openai = OpenAI(api_key=getattr(settings, "openai_api_key", None))
        self.embedding_model = (
            getattr(settings, "embedding_model", None)
            or os.getenv("EMBEDDING_MODEL")
            or "text-embedding-3-small"
        )
        self.max_chunk_chars = int(os.getenv("KB_CHUNK_CHARS", "1200"))
        self.chunk_overlap = int(os.getenv("KB_CHUNK_OVERLAP", "150"))
        self.top_k = int(os.getenv("KB_TOP_K", "8"))

        # Яндекс.Диск
        self.y_token = getattr(settings, "yandex_disk_token", None) or getattr(settings, "yadisk_token", None)
        self.root = (
            getattr(settings, "yandex_root_path", None)
            or getattr(settings, "yadisk_folder", None)
            or "disk:/"
        )
        self._yd_client = None
        if self.y_token and yadisk is not None:
            try:
                self._yd_client = yadisk.Client(token=self.y_token)
            except Exception as e:
                logger.warning("KB Retriever: yadisk client init failed: %s", e)

        # Манифест (метаданные по кэшу)
        self.manifest: Dict[str, Dict] = {}
        self._load_manifest()

    # -------- manifest --------

    def _load_manifest(self) -> None:
        if not MANIFEST_PATH.exists():
            logger.warning("KB Retriever: manifest.json not found, creating empty.")
            self.manifest = {}
            self._save_manifest()
            return
        try:
            self.manifest = json.loads(MANIFEST_PATH.read_text("utf-8"))
        except Exception as e:
            logger.warning("KB Retriever: manifest load failed: %s — recreating empty.", e)
            self.manifest = {}
            self._save_manifest()

    def _save_manifest(self) -> None:
        try:
            MANIFEST_PATH.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), "utf-8")
        except Exception as e:
            logger.warning("KB Retriever: manifest save failed: %s", e)

    # -------- public API --------

    def retrieve(self, question: str, selected_paths: List[str]) -> List[KBChunk]:
        """
        Возвращает список релевантных чанков по вопросу из выбранных документов.
        """
        if not selected_paths:
            return []

        all_chunks: List[KBChunk] = []
        for path in selected_paths:
            try:
                chunks = self._ensure_chunks_for_path(path)
                all_chunks.extend(chunks)
            except Exception as e:
                logger.warning("KB Retriever: ensure chunks failed for %s: %s", path, e)

        if not all_chunks:
            return []

        # Если нет numpy — просто вернем первые N чанков каждого файла (без ранжирования).
        if np is None:
            logger.warning("KB Retriever: numpy not available — return naive top chunks.")
            return all_chunks[: self.top_k]

        try:
            q_emb = self._embed_texts([question])[0]
        except Exception as e:
            logger.warning("KB Retriever: question embedding failed: %s — return naive chunks.", e)
            return all_chunks[: self.top_k]

        # Ранжируем по косинусу
        for ch in all_chunks:
            emb = self._load_chunk_embedding(ch.path, ch.chunk_id)
            if emb is None:
                ch.score = 0.0
            else:
                ch.score = float(self._cosine(q_emb, emb))

        all_chunks.sort(key=lambda c: c.score, reverse=True)
        return all_chunks[: self.top_k]

    # -------- internal: chunks/embeddings --------

    def _cache_base(self, path: str) -> Path:
        # безопасное имя файла
        safe = path.replace("/", "_").replace(":", "_")
        return CACHE_DIR / safe

    def _ensure_chunks_for_path(self, path: str) -> List[KBChunk]:
        """
        Проверяет кэш; если нет — скачивает PDF -> извлекает текст -> чанки -> эмбеддинги -> сохраняет.
        """
        base = self._cache_base(path)
        text_file = base.with_suffix(".txt")
        chunks_file = base.with_suffix(".chunks.json")

        # 1) если есть чанки — читаем
        if chunks_file.exists():
            chunks = self._load_chunks_from_file(path, chunks_file)
            if chunks:
                return chunks

        # 2) иначе — собрать заново
        raw_bytes = self._download_from_yadisk(path)
        if not raw_bytes:
            logger.warning("KB Retriever: file download empty for %s", path)
            return []

        if PdfReader is None:
            logger.warning("KB Retriever: pypdf not installed — cannot extract text from PDF.")
            return []

        text = self._extract_text_from_pdf(raw_bytes)
        if not text:
            logger.warning("KB Retriever: empty text extracted for %s", path)
            return []

        try:
            text_file.write_text(text, "utf-8")
        except Exception:
            pass

        chunks = self._chunk_text(path, text)

        # эмбеддинги
        try:
            embeds = self._embed_texts([c.text for c in chunks])
        except Exception as e:
            logger.warning("KB Retriever: embedding failed for %s: %s", path, e)
            embeds = []

        if embeds and np is not None:
            for i, emb in enumerate(embeds):
                self._save_chunk_embedding(path, i, emb)

        self._save_chunks_to_file(chunks_file, chunks)

        # обновим манифест
        self.manifest[path] = {
            "chunks": len(chunks),
            "updated_at": int(time.time()),
        }
        self._save_manifest()

        return chunks

    def _extract_text_from_pdf(self, pdf_bytes: bytes) -> str:
        out_parts: List[str] = []
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            for page in reader.pages:
                try:
                    txt = page.extract_text() or ""
                except Exception:
                    txt = ""
                if txt:
                    out_parts.append(txt)
        except Exception as e:
            logger.warning("KB Retriever: PdfReader failed: %s", e)
        return "\n".join(out_parts).strip()

    def _chunk_text(self, path: str, text: str) -> List[KBChunk]:
        """
        Очень простой нарезчик по символам c overlap.
        """
        chunks: List[KBChunk] = []
        n = len(text)
        if n == 0:
            return chunks

        step = max(1, self.max_chunk_chars - self.chunk_overlap)
        idx = 0
        cid = 0
        while idx < n:
            piece = text[idx : idx + self.max_chunk_chars]
            chunks.append(KBChunk(path=path, chunk_id=cid, text=piece))
            cid += 1
            idx += step
        logger.info("KB Retriever: %s -> %d chunks", path, len(chunks))
        return chunks

    # ---- embeddings ----

    def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        Обёртка над OpenAI embeddings: возвращает список векторов.
        """
        if not texts:
            return []
        resp = self.openai.embeddings.create(model=self.embedding_model, input=texts)
        return [item.embedding for item in resp.data]

    def _save_chunk_embedding(self, path: str, chunk_id: int, emb: List[float]) -> None:
        base = self._cache_base(path)
        emb_path = base.with_suffix(f".emb.{chunk_id}.npy")
        if np is None:
            return
        try:
            np.save(emb_path, np.array(emb, dtype=np.float32))
        except Exception as e:
            logger.debug("KB Retriever: save embedding failed for %s #%d: %s", path, chunk_id, e)

    def _load_chunk_embedding(self, path: str, chunk_id: int) -> Optional["np.ndarray"]:
        if np is None:
            return None
        base = self._cache_base(path)
        emb_path = base.with_suffix(f".emb.{chunk_id}.npy")
        if not emb_path.exists():
            return None
        try:
            return np.load(emb_path)
        except Exception:
            return None

    def _cosine(self, a: "np.ndarray", b: "np.ndarray") -> float:
        if np is None:
            return 0.0
        denom = (np.linalg.norm(a) * np.linalg.norm(b))
        if denom == 0:
            return 0.0
        return float(np.dot(a, b) / denom)

    # ---- chunks IO ----

    def _save_chunks_to_file(self, path: Path, chunks: List[KBChunk]) -> None:
        try:
            data = [{"path": c.path, "chunk_id": c.chunk_id, "text": c.text} for c in chunks]
            path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        except Exception as e:
            logger.debug("KB Retriever: save chunks failed: %s", e)

    def _load_chunks_from_file(self, doc_path: str, path: Path) -> List[KBChunk]:
        try:
            data = json.loads(path.read_text("utf-8"))
            out: List[KBChunk] = []
            for obj in data:
                out.append(
                    KBChunk(
                        path=obj.get("path", doc_path),
                        chunk_id=int(obj.get("chunk_id", 0)),
                        text=obj.get("text", ""),
                    )
                )
            logger.info("KB Retriever: loaded %d chunks from cache for %s", len(out), doc_path)
            return out
        except Exception as e:
            logger.debug("KB Retriever: load chunks failed: %s", e)
            return []

    # ---- download from Yandex.Disk ----

    def _download_from_yadisk(self, path: str) -> bytes:
        if not self._yd_client:
            logger.warning("KB Retriever: Yandex.Disk client not initialized.")
            return b""
        try:
            buf = io.BytesIO()
            self._yd_client.download(path, buf)  # type: ignore
            return buf.getvalue()
        except Exception as e:
            logger.warning("KB Retriever: download failed for %s: %s", path, e)
            return b""
