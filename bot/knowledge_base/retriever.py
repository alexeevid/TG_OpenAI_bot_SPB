# bot/knowledge_base/retriever.py
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Iterable, List, Optional, TypedDict

logger = logging.getLogger(__name__)


class KBChunk(TypedDict, total=False):
    """Тип единицы контента, который возвращает ретривер."""
    text: str
    source: str
    page: Optional[int]
    score: float


class KnowledgeBaseRetriever:
    """
    Ретривер фрагментов из Базы знаний.

    Источник по умолчанию — JSON-манифест вида:
    {
      "chunks": [
        {"text": "...", "source": "disk:/База/.../file.pdf", "page": 12},
        ...
      ]
    }

    Если появится векторный поиск, реализуйте _vector_search()
    и установите self._vector_ready = True.
    """

    def __init__(self, settings):
        # Путь к манифесту: env KB_MANIFEST_PATH, Settings.kb_manifest_path или data/kb/manifest.json
        self.manifest_path: str = (
            getattr(settings, "kb_manifest_path", None)
            or os.getenv("KB_MANIFEST_PATH")
            or os.path.join("data", "kb", "manifest.json")
        )

        # Флаг готовности векторного поиска (по умолчанию выключен)
        self._vector_ready: bool = False

        # Данные манифеста в памяти
        self._manifest: Dict = {}

        # Загружаем/создаём манифест
        self._ensure_manifest()

    # -------------------- Публичные методы --------------------

    def refresh_manifest(self) -> None:
        """Принудительно перечитать manifest.json с диска."""
        self._load_manifest()

    def retrieve(
        self,
        query: str,
        selected_docs: Optional[List[str]] = None,
        top_k: int = 8,
        min_score: float = 0.30,
    ) -> List[KBChunk]:
        """
        Возвращает список чанков:
        { "text": str, "source": str, "page": Optional[int], "score": float }

        - Фильтр по selected_docs (если задан)
        - Если есть векторный поиск — используем его (top_k с запасом)
        - Иначе — фолбэк: подстрочный скоринг + лёгкие эвристики
        - Отсечка по min_score и ограничение top_k
        """
        q = (query or "").strip()
        if not q:
            return []

        sel_set = set(selected_docs or [])

        # 1) Корпус кандидатов
        corpus: List[KBChunk] = []
        for ch in self._iter_all_chunks():
            src = ch.get("source") or "unknown"
            if sel_set and src not in sel_set:
                continue
            text = ch.get("text") or ""
            page = ch.get("page")
            corpus.append({"text": text, "source": src, "page": page})

        if not corpus:
            return []

        results: List[KBChunk] = []

        # 2) Векторный поиск (если включён)
        if self._vector_ready and hasattr(self, "_vector_search"):
            try:
                vec_hits = self._vector_search(q, selected_docs=sel_set, top_k=top_k * 2)  # небольшой запас
                for h in vec_hits or []:
                    results.append({
                        "text": getattr(h, "text", "") or h.get("text", ""),
                        "source": getattr(h, "source", None) or h.get("source", "unknown"),
                        "page": getattr(h, "page", None) or h.get("page", None),
                        "score": float(getattr(h, "score", 0.0) or h.get("score", 0.0)),
                    })
            except Exception as e:
                logger.warning("Vector search failed, fallback to substring scoring: %s", e)
                results = []

        # 3) Фолбэк-скоринг
        if not results:
            q_low = q.lower()

            # Простое расширение ключей для акронимов (пример WoW)
            expand_keys: List[str] = [q_low]
            if len(q.split()) <= 4:
                if "wow" in q_low:
                    expand_keys += [
                        "choose your wow",
                        "ways of working",
                        "wow definition",
                        "disciplined agile wow",
                        "choose your way of working",
                    ]

            scored: List[KBChunk] = []
            for ch in corpus:
                text_low = (ch["text"] or "").lower()
                score = 0.0

                # Частотный подстрочный скоринг по расширенным ключам
                for k in expand_keys:
                    if k and k in text_low:
                        score += text_low.count(k) * 0.6

                # Бонусы за «определенческие» паттерны
                if "wow" in q_low:
                    if "choose your wow" in text_low:
                        score += 0.8
                    if "wow —" in text_low or "wow -" in text_low or "wow (" in text_low:
                        score += 1.0

                if score > 0:
                    c = dict(ch)  # copy
                    c["score"] = score  # type: ignore[typeddict-item]
                    scored.append(c)    # type: ignore[arg-type]

            # Нормировка 0..1
            if scored:
                m = max(s["score"] for s in scored)  # type: ignore[index]
                if m > 0:
                    for s in scored:
                        s["score"] = s["score"] / m  # type: ignore[index]
                results = scored

        # 4) Сортировка и отсечка
        results.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
        results = [r for r in results if float(r.get("score", 0.0)) >= min_score][:top_k]

        return results

    # -------------------- Внутренние функции --------------------

    def _ensure_manifest(self) -> None:
        """Проверяет наличие manifest.json и загружает/создаёт его."""
        try:
            self._load_manifest()
        except FileNotFoundError:
            os.makedirs(os.path.dirname(self.manifest_path), exist_ok=True)
            self._manifest = {"chunks": []}
            with open(self.manifest_path, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, ensure_ascii=False, indent=2)
            logger.warning("KB Retriever: manifest.json not found, creating empty.")
        except Exception as e:
            logger.warning("KB Retriever: failed to load manifest: %s", e)
            self._manifest = {"chunks": []}

    def _load_manifest(self) -> None:
        """Читает manifest.json с диска в self._manifest."""
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            self._manifest = json.load(f)
        if not isinstance(self._manifest, dict):
            self._manifest = {"chunks": []}

    def _iter_all_chunks(self) -> Iterable[KBChunk]:
        """
        Итерирует по всем чанкам корпуса из манифеста.
        Каждый чанк — словарь: {text, source, page}.
        """
        chunks = (self._manifest or {}).get("chunks", []) or []
        for item in chunks:
            if not isinstance(item, dict):
                continue
            yield {
                "text": item.get("text", "") or "",
                "source": item.get("source", "unknown") or "unknown",
                "page": item.get("page"),
            }

    # --------- Заглушка под векторный поиск ---------

    def _vector_search(
        self,
        query: str,
        selected_docs: Optional[set] = None,
        top_k: int = 8,
    ) -> List[KBChunk]:
        """
        Если подключите pgvector/FAISS, реализуйте реальный поиск
        и выставляйте self._vector_ready = True в __init__.
        """
        return []
