# bot/knowledge_base/retriever.py
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


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

    Если доступен векторный поиск, реализуйте _vector_search()
    и выставьте self._vector_ready = True.
    """

    def __init__(self, settings):
        # Путь к манифесту можно задать переменной окружения или в settings
        self.manifest_path: str = (
            getattr(settings, "kb_manifest_path", None)
            or os.getenv("KB_MANIFEST_PATH")
            or os.path.join("data", "kb", "manifest.json")
        )

        # Признак готовности векторного поиска (по умолчанию нет)
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
    ) -> List[dict]:
        """
        Возвращает список чанков вида:
        { "text": str, "source": str, "page": Optional[int], "score": float }

        - Фильтр по selected_docs (если задан)
        - Сначала пробуем векторный поиск (если включён)
        - При недоступности векторов — подстрочный скоринг с эвристиками
        - Отсечка по min_score и ограничение top_k
        """
        q = (query or "").strip()
        if not q:
            return []

        sel_set = set(selected_docs or [])

        # 1) Собираем корпус кандидатов (все или только выбранные документы)
        corpus: List[dict] = []
        for ch in self._iter_all_chunks():
            src = ch.get("source") or "unknown"
            if sel_set and src not in sel_set:
                continue
            text = ch.get("text") or ""
            page = ch.get("page")
            corpus.append({"text": text, "source": src, "page": page})

        if not corpus:
            return []

        results: List[dict] = []

        # 2) Попытка векторного поиска (если реализован)
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

        # 3) Фолбэк: подстрочный/эвристический скоринг
        if not results:
            q_low = q.lower()

            # Простое расширение ключей для акронимов (пример WoW)
            expand_keys: List[str] = [q_low]
            if len(q.split()) <= 4:
                if "wow" in q_low:
                    expand_keys += [
                        "choose your wow",            # полное название книги/метода
                        "ways of working",            # расшифровка акронима в контексте DA
                        "wow definition",             # подсказка для определений в тексте
                        "disciplined agile wow",      # уточнение контекста
                        "choose your way of working", # альтернативная формулировка
                    ]

            scored: List[dict] = []
            for ch in corpus:
                text_low = ch["text"].lower()
                score = 0.0

                # Частотный подстрочный скоринг по расширенным ключам
                for k in expand_keys:
                    if not k:
                        continue
                    if k in text_low:
                        score += text_low.count(k) * 0.6

                # Бонусы за «определенческие» паттерны
                if "wow" in q_low:
                    if "wow —" in text_low or "wow -" in text_low or "wow (" in text_low:
                        score += 1.0
                    if "choose your wow" in text_low:
                        score += 0.8

                if score > 0:
                    scored.append({**ch, "score": score})

            # Нормировка 0..1
            if scored:
                m = max(s["score"] for s in scored)
                if m > 0:
                    for s in scored:
                        s["score"] = s["score"] / m
                results = scored

        # 4) Сортировка и отсечка
        results.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        results = [r for r in results if r.get("score", 0.0) >= min_score][:top_k]

        return results

    # -------------------- Внутренние функции --------------------

    def _ensure_manifest(self) -> None:
        """Проверяет наличие manifest.json и загружает/создаёт его."""
        try:
            self._load_manifest()
        except FileNotFoundError:
            # Создадим пустой манифест и папку
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

    def _iter_all_chunks(self) -> Iterable[dict]:
        """
        Итерирует по всем чанкам корпуса из манифеста.
        Каждый чанк — словарь: {text, source, page}.
        """
        chunks = (self._manifest or {}).get("chunks", []) or []
        for item in chunks:
            # Защита от кривых записей
            if not isinstance(item, dict):
                continue
            yield {
                "text": item.get("text", "") or "",
                "source": item.get("source", "unknown") or "unknown",
                "page": item.get("page"),
            }

    # --------- Заглушка под векторный поиск (опционально подключаемая) ---------

    def _vector_search(
        self,
        query: str,
        selected_docs: Optional[set] = None,
        top_k: int = 8,
    ) -> List[dict]:
        """
        Заглушка. Если у вас есть БД с pgvector/FAISS и т.п.,
        реализуйте здесь реальный поиск и выставляйте self._vector_ready = True
        в __init__ при успешной инициализации движка.

        Возвращаемый формат списка:
        [{ "text": str, "source": str, "page": Optional[int], "score": float }, ...]
        """
        # По умолчанию — нет векторного поиска.
        return []
