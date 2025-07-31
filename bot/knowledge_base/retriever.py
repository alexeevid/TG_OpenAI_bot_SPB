# bot/knowledge_base/retriever.py
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# опционально pgvector
try:
    import sqlalchemy as sa
    from sqlalchemy import text
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False


@dataclass
class KBChunk:
    doc_id: str
    snippet: str
    score: float
    meta: dict  # { "title": "...", "page": 12, ... }


class KnowledgeBaseRetriever:
    """
    Универсальный ретривер.
    1) Пытается использовать Postgres+pgvector: таблица kb_chunks (id, doc_id, title, snippet, embedding vector)
    2) Фолбэк: on-disk индекс в data/kb_index.npz + data/kb_meta.json

    Индексацию чанков организует IndexBuilder (см. ниже) — можно вызывать через ensure_index().
    Здесь мы только читаем и ищем.
    """

    def __init__(self, settings, data_dir: str = "data"):
        self.settings = settings
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

        self.db_url = getattr(settings, "database_url", getattr(settings, "DATABASE_URL", None))
        self.use_pg = False
        self._engine = None

        if HAVE_SA and self.db_url:
            try:
                self._engine = sa.create_engine(self.db_url, pool_pre_ping=True)
                with self._engine.connect() as con:
                    # проверим расширение pgvector
                    rows = con.execute(text("SELECT extname FROM pg_extension;")).fetchall()
                    have_pgvector = any(r[0] == "vector" for r in rows)
                    if have_pgvector:
                        # создадим таблицу при необходимости
                        con.execute(text("""
                        CREATE TABLE IF NOT EXISTS kb_chunks (
                            id BIGSERIAL PRIMARY KEY,
                            doc_id TEXT NOT NULL,
                            title TEXT,
                            snippet TEXT,
                            embedding vector(1536)
                        );
                        """))
                        # индексы
                        con.execute(text("CREATE INDEX IF NOT EXISTS kb_chunks_doc_id_idx ON kb_chunks(doc_id);"))
                        self.use_pg = True
                        logger.info("KB Retriever: using Postgres+pgvector")
                    else:
                        logger.warning("KB Retriever: pgvector extension not found, fallback to on-disk")
            except Exception as e:
                logger.warning("KB Retriever: DB init failed: %s, fallback to on-disk", e)

        # on-disk fallback
        self._npz_path = os.path.join(self.data_dir, "kb_index.npz")
        self._meta_path = os.path.join(self.data_dir, "kb_meta.json")
        self._mem_vecs: Optional[np.ndarray] = None
        self._mem_meta: Optional[List[dict]] = None

    # ---------- Public API ----------

    def ensure_index(self) -> None:
        """
        Проверяет, что индекс существует.
        Здесь мы ничего не строим (строит IndexBuilder ниже),
        но подгружаем on-disk в память при необходимости.
        """
        if self.use_pg:
            return
        if os.path.exists(self._npz_path) and os.path.exists(self._meta_path):
            try:
                with np.load(self._npz_path) as npz:
                    self._mem_vecs = npz["embeddings"]
                with open(self._meta_path, "r", encoding="utf-8") as f:
                    self._mem_meta = json.load(f)
                logger.info("KB Retriever: loaded on-disk index: %s vectors", len(self._mem_meta))
            except Exception as e:
                logger.warning("KB Retriever: failed to load on-disk index: %s", e)
                self._mem_vecs = None
                self._mem_meta = None

    def retrieve(self, query: str, doc_ids: Sequence[str], embedder, top_k: int = 8) -> List[KBChunk]:
        """
        query -> embedding -> top_k похожих чанков из указанных документов (doc_ids).
        embedder: callable(list[str]) -> list[list[float]]
        """
        if not doc_ids:
            return []

        q_emb = np.array(embedder([query])[0], dtype=np.float32)

        if self.use_pg:
            try:
                return self._retrieve_pg(q_emb, doc_ids, top_k)
            except Exception as e:
                logger.warning("KB Retriever: pg query failed, fallback to disk: %s", e)

        # disk fallback
        return self._retrieve_disk(q_emb, set(doc_ids), top_k)

    # ---------- Backends ----------

    def _retrieve_pg(self, q_emb: np.ndarray, doc_ids: Sequence[str], top_k: int) -> List[KBChunk]:
        # В PG Вектор длина 1536 (text-embedding-3-small)
        emb_list = ",".join([str(float(x)) for x in q_emb.tolist()])
        doc_ids_sql = ",".join(["'%s'" % d.replace("'", "''") for d in doc_ids])
        sql = f"""
            SELECT doc_id, title, snippet,
                   1 - (embedding <=> '[{emb_list}]') AS score
            FROM kb_chunks
            WHERE doc_id IN ({doc_ids_sql})
            ORDER BY embedding <-> '[{emb_list}]'
            LIMIT :top_k;
        """
        out: List[KBChunk] = []
        with self._engine.connect() as con:
            rows = con.execute(text(sql), {"top_k": top_k}).fetchall()
            for r in rows:
                out.append(KBChunk(
                    doc_id=r[0],
                    snippet=r[2] or "",
                    score=float(r[3]),
                    meta={"title": r[1] or "", "source": r[0]},
                ))
        return out

    def _retrieve_disk(self, q_emb: np.ndarray, doc_ids_set: set, top_k: int) -> List[KBChunk]:
        if self._mem_vecs is None or self._mem_meta is None:
            self.ensure_index()
        if self._mem_vecs is None or self._mem_meta is None or len(self._mem_meta) == 0:
            return []

        # фильтруем по doc_ids
        idxs = [i for i, m in enumerate(self._mem_meta) if m.get("doc_id") in doc_ids_set]
        if not idxs:
            return []

        vecs = self._mem_vecs[idxs]
        metas = [self._mem_meta[i] for i in idxs]

        # косинусная близость: sim = dot(a,b)/(||a||*||b||)
        denom = (np.linalg.norm(vecs, axis=1) * np.linalg.norm(q_emb) + 1e-9)
        sims = vecs @ q_emb / denom

        order = np.argsort(-sims)[:top_k]
        out: List[KBChunk] = []
        for i in order:
            m = metas[i]
            out.append(KBChunk(
                doc_id=m["doc_id"],
                snippet=m.get("snippet", "")[:1000],
                score=float(sims[i]),
                meta={"title": m.get("title", ""), "page": m.get("page"), "source": m.get("doc_id")},
            ))
        return out


# --------- Индексатор чанков (простая реализация) ---------

class IndexBuilder:
    """
    Строит индекс чанков для выбранных документов.
    Пытается извлечь текст (pdf/docx/txt). Если нет зависимостей — берёт только заголовок.
    Сохраняет либо в PG (kb_chunks), либо on-disk (npz+json).
    """

    def __init__(self, settings, retriever: KnowledgeBaseRetriever):
        self.settings = settings
        self.retriever = retriever
        self.data_dir = retriever.data_dir
        os.makedirs(self.data_dir, exist_ok=True)

        # on-disk
        self._npz_path = os.path.join(self.data_dir, "kb_index.npz")
        self._meta_path = os.path.join(self.data_dir, "kb_meta.json")

        # Download helper (Yandex.Disk)
        self.ya_token = getattr(settings, "yadisk_token", None)
        self.kb_root = getattr(settings, "kb_root", "disk:/База Знаний")
        self.local_kb_dir = getattr(settings, "kb_local_dir", None)

        self._yadisk = None
        try:
            import yadisk  # noqa
            if self.ya_token:
                self._yadisk = yadisk.YaDisk(token=self.ya_token)
        except Exception:
            pass

    # --- Public ---

    def build_for_docs(self, docs: List[dict], embedder, chunk_chars: int = 1200, max_chunks_per_doc: int = 200) -> Tuple[int, int]:
        """
        Создаёт/обновляет индекс для списка документов.
        docs: [{"doc_id": "...", "title": "...", "mime": "..."}]
        Возвращает (processed_docs, total_chunks)
        """
        texts: List[str] = []
        metas: List[dict] = []

        processed_docs = 0
        total_chunks = 0

        for d in docs:
            doc_id = d["doc_id"]
            title = d.get("title") or os.path.basename(doc_id)
            try:
                content = self._load_text(doc_id, d.get("mime"))
                if not content:
                    # хотя бы индексируем title
                    chunks = [title]
                else:
                    chunks = self._split_text(content, chunk_chars)[:max_chunks_per_doc]
                for ch in chunks:
                    texts.append(ch)
                    metas.append({"doc_id": doc_id, "title": title, "snippet": ch[:1000]})
                processed_docs += 1
                total_chunks += len(chunks)
            except Exception as e:
                logger.warning("IndexBuilder: failed to process %s: %s", doc_id, e)

        if not texts:
            return processed_docs, 0

        embs = np.array(embedder(texts), dtype=np.float32)  # N x 1536

        if self.retriever.use_pg:
            self._save_to_pg(metas, embs)
        else:
            self._save_to_disk(metas, embs)

        return processed_docs, total_chunks

    # --- Helpers ---

    def _resolve_local_path(self, doc_id: str) -> Optional[str]:
        if doc_id.startswith("file://"):
            return doc_id[len("file://"):]
        return None

    def _download_from_yadisk(self, path: str) -> bytes:
        # выгрузка в память
        if not self._yadisk:
            raise RuntimeError("Yandex.Disk client not available")
        link = self._yadisk.get_download_link(path)
        import httpx
        r = httpx.get(link, follow_redirects=True, timeout=60)
        r.raise_for_status()
        return r.content

    def _load_text(self, doc_id: str, mime: Optional[str]) -> Optional[str]:
        """
        Пытаемся извлечь текст. Все импорты — lazy, чтобы не падать без зависимостей.
        Если извлечь нельзя — вернём None.
        """
        path = self._resolve_local_path(doc_id)
        data = None
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
        elif doc_id.startswith("disk:/"):
            try:
                data = self._download_from_yadisk(doc_id)
            except Exception as e:
                logger.warning("IndexBuilder: YD download failed for %s: %s", doc_id, e)
                return None
        else:
            return None

        if not mime:
            mime = self._guess_mime(doc_id)

        try:
            if mime == "application/pdf" or doc_id.lower().endswith(".pdf"):
                # попытка pypdf
                try:
                    from pypdf import PdfReader  # type: ignore
                    import io
                    reader = PdfReader(io.BytesIO(data))
                    pages = [p.extract_text() or "" for p in reader.pages]
                    return "\n\n".join(pages)
                except Exception:
                    return None
            if mime.endswith("wordprocessingml.document") or doc_id.lower().endswith(".docx"):
                try:
                    import io
                    import docx  # python-docx
                    doc = docx.Document(io.BytesIO(data))
                    return "\n".join(p.text for p in doc.paragraphs)
                except Exception:
                    return None
            if mime == "text/plain" or doc_id.lower().endswith((".txt", ".md")):
                try:
                    return data.decode("utf-8", errors="ignore")
                except Exception:
                    return None
        except Exception:
            return None

        return None

    @staticmethod
    def _split_text(text: str, chunk_chars: int) -> List[str]:
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        out: List[str] = []
        i = 0
        n = len(text)
        while i < n:
            j = min(i + chunk_chars, n)
            # постараемся не резать слово
            if j < n:
                k = text.rfind(" ", i, j)
                if k > i + chunk_chars // 2:
                    j = k
            out.append(text[i:j])
            i = j
        return out

    def _save_to_pg(self, metas: List[dict], embs: np.ndarray) -> None:
        import sqlalchemy as sa
        from sqlalchemy import text

        with self.retriever._engine.begin() as con:
            # очистим старые чанки по doc_id, которые индексируем
            doc_ids = {m["doc_id"] for m in metas}
            if doc_ids:
                doc_ids_sql = ",".join(["'%s'" % d.replace("'", "''") for d in doc_ids])
                con.execute(text(f"DELETE FROM kb_chunks WHERE doc_id IN ({doc_ids_sql});"))

            # вставка
            for m, v in zip(metas, embs):
                emb_list = ",".join([str(float(x)) for x in v.tolist()])
                con.execute(text("""
                    INSERT INTO kb_chunks (doc_id, title, snippet, embedding)
                    VALUES (:doc_id, :title, :snippet, :embedding::vector)
                """), {
                    "doc_id": m["doc_id"],
                    "title": m.get("title") or "",
                    "snippet": m.get("snippet") or "",
                    "embedding": f"[{emb_list}]",
                })

    def _save_to_disk(self, metas: List[dict], embs: np.ndarray) -> None:
        # загружаем существующие, удаляем старые для тех же doc_id и добавляем
        existing_vecs = None
        existing_meta = []
        if os.path.exists(self._meta_path) and os.path.exists(self._npz_path):
            try:
                with np.load(self._npz_path) as npz:
                    existing_vecs = npz["embeddings"]
                with open(self._meta_path, "r", encoding="utf-8") as f:
                    existing_meta = json.load(f)
            except Exception:
                existing_vecs = None
                existing_meta = []

        if existing_vecs is not None and len(existing_meta) == existing_vecs.shape[0]:
            # фильтруем старые по doc_id
            doc_ids = {m["doc_id"] for m in metas}
            keep_idx = [i for i, m in enumerate(existing_meta) if m.get("doc_id") not in doc_ids]
            if keep_idx:
                existing_vecs = existing_vecs[keep_idx]
                existing_meta = [existing_meta[i] for i in keep_idx]
            else:
                existing_vecs = None
                existing_meta = []

        if existing_vecs is None:
            all_vecs = embs
            all_meta = metas
        else:
            all_vecs = np.concatenate([existing_vecs, embs], axis=0)
            all_meta = existing_meta + metas

        # сохраняем
        np.savez_compressed(self._npz_path, embeddings=all_vecs)
        with open(self._meta_path, "w", encoding="utf-8") as f:
            json.dump(all_meta, f, ensure_ascii=False)
        logger.info("IndexBuilder: on-disk index saved: %s vectors", all_vecs.shape[0])

    @staticmethod
    def _guess_mime(path: str) -> str:
        p = path.lower()
        if p.endswith(".pdf"):
            return "application/pdf"
        if p.endswith(".docx"):
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if p.endswith(".txt") or p.endswith(".md"):
            return "text/plain"
        return "application/octet-stream"
