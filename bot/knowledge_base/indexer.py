# bot/knowledge_base/indexer.py
from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Tuple

from .types import KBDocument

logger = logging.getLogger(__name__)


def _default_kb_dir(settings) -> str:
    """
    Каталог, где лежит индекс БЗ (manifest.json).
    На Railway удобно указывать volume: /data/kb
    """
    return getattr(settings, "kb_index_dir", "/data/kb")


class KnowledgeBaseIndexer:
    """
    Упрощённый индексер:
    - Хранит manifest.json (список документов и чанков).
    - Метод sync() возвращает счетчики изменений.
    Здесь демо-реализация (ничего не меняет), чтобы починить ваш UI и импорты.
    Реальную синхронизацию с Яндекс.Диском/фс можно подключить позже, главное —
    поддерживать формат manifest.json, который читает retriever.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.kb_dir = _default_kb_dir(settings)
        self.manifest_path = os.path.join(self.kb_dir, "manifest.json")

        os.makedirs(self.kb_dir, exist_ok=True)
        if not os.path.isfile(self.manifest_path):
            with open(self.manifest_path, "w", encoding="utf-8") as f:
                json.dump({"documents": []}, f, ensure_ascii=False, indent=2)

    # --------- чтение/запись манифеста ---------

    def _load_manifest(self) -> Dict:
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "documents" not in data or not isinstance(data["documents"], list):
                data = {"documents": []}
        except Exception:
            data = {"documents": []}
        return data

    def _save_manifest(self, data: Dict) -> None:
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # --------- API, которое вызывает бот ---------

    def sync(self) -> Tuple[int, int, int, int]:
        """
        Синхронизация состава документов.
        Возвращает: (added, updated, deleted, unchanged)

        Демо: ничего не меняем и считаем, что всё "без изменений".
        Подключите сюда вашу логику с Я.Диском: вычислите дельту и
        обновите data["documents"], затем сохраните через _save_manifest().
        """
        data = self._load_manifest()

        added = updated = deleted = 0
        unchanged = len(data.get("documents", []))

        self._save_manifest(data)
        return added, updated, deleted, unchanged

    def list_documents(self) -> List[KBDocument]:
        data = self._load_manifest()
        out: List[KBDocument] = []
        for d in data.get("documents", []):
            out.append(
                KBDocument(
                    ext_id=d.get("ext_id") or d.get("id") or d.get("path") or d.get("title", "doc"),
                    title=d.get("title") or os.path.basename(d.get("path", "")) or "Документ",
                    source_path=d.get("path"),
                    size_bytes=d.get("size_bytes"),
                    mtime_ts=d.get("mtime_ts"),
                )
            )
        return out
