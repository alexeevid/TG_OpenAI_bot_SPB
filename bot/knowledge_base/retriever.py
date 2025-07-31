# bot/knowledge_base/retriever.py
from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, List, Optional

from .types import KBDocument, KBChunk

logger = logging.getLogger(__name__)


def _default_kb_dir(settings) -> str:
    return getattr(settings, "kb_index_dir", "/data/kb")


class KnowledgeBaseRetriever:
    """
    Упрощённый retriever без pgvector:
    - читает manifest.json, созданный индексером;
    - отдаёт список документов для UI;
    - делает очень простой keyword-score по чанкам.
    Позже сюда легко встраивается путь с pgvector (как приоритетный).
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.kb_dir = _default_kb_dir(settings)
        self.manifest_path = os.path.join(self.kb_dir, "manifest.json")
        self._manifest_cache: Optional[Dict] = None

        os.makedirs(self.kb_dir, exist_ok=True)
        if not os.path.isfile(self.manifest_path):
            logger.warning("KB Retriever: manifest.json not found, creating empty.")
            with open(self.manifest_path, "w", encoding="utf-8") as f:
                json.dump({"documents": []}, f, ensure_ascii=False, indent=2)

    # ---------- внутреннее ----------

    def _load_manifest(self) -> Dict:
        if self._manifest_cache is not None:
            return self._manifest_cache
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "documents" not in data or not isinstance(data["documents"], list):
                data = {"documents": []}
        except Exception as e:
            logger.exception("KB Retriever: failed to read manifest: %s", e)
            data = {"documents": []}
        self._manifest_cache = data
        return data

    # ---------- API для бота ----------

    def list_documents(self) -> List[KBDocument]:
        data = self._load_manifest()
        docs: List[KBDocument] = []
        for d in data["documents"]:
            try:
                docs.append(
                    KBDocument(
                        ext_id=d.get("ext_id") or d.get("id") or d.get("path") or d.get("title", "doc"),
                        title=d.get("title") or os.path.basename(d.get("path", "")) or "Документ",
                        source_path=d.get("path"),
                        size_bytes=d.get("size_bytes"),
                        mtime_ts=d.get("mtime_ts"),
                    )
                )
            except Exception as e:
                logger.warning("KB Retriever: skip bad doc entry: %s (err=%s)", d, e)
        return docs

    def retrieve(self, query: str, selected_ext_ids: List[str], top_k: Optional[int] = None) -> List[KBChunk]:
        """
        Простая релевантность без векторов:
        - токенизируем запрос на ключевые слова
        - ранжируем чанки по количеству вхождений
        """
        if not query.strip() or not selected_ext_ids:
            return []

        top_k = top_k or int(getattr(self.settings, "rag_top_k", 5))
        data = self._load_manifest()
        words = self._extract_words(query)
        if not words:
            return []

        results: List[KBChunk] = []
        for d in data["documents"]:
            ext_id = d.get("ext_id") or d.get("id") or d.get("path")
            if not ext_id or ext_id not in selected_ext_ids:
                continue
            title = d.get("title") or "Документ"
            source_path = d.get("path")

            for ch in d.get("chunks", []):
                text = ch.get("content") or ""
                page = ch.get("page")
                score = self._score(text, words)
                if score > 0:
                    results.append(
                        KBChunk(
                            ext_id=ext_id,
                            title=title,
                            content=text,
                            score=float(score),
                            page=page,
                            source_path=source_path,
                        )
                    )

        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]

    # ---------- utils ----------

    _word_re = re.compile(r"[A-Za-zА-Яа-я0-9]+")

    def _extract_words(self, text: str) -> List[str]:
        return [w.lower() for w in self._word_re.findall(text)]

    def _score(self, text: str, words: List[str]) -> int:
        if not text:
            return 0
        lt = text.lower()
        s = 0
        for w in words:
            s += lt.count(w)
        return s
