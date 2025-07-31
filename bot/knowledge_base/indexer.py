# bot/knowledge_base/indexer.py
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple

import yadisk

logger = logging.getLogger(__name__)


@dataclass
class KBMeta:
    id: str
    title: str
    path: str
    encrypted: bool
    updated_at: datetime | None
    pages: int | None = None


class KnowledgeBaseIndexer:
    """
    Минимально-инвазивная реализация синхронизации с папкой на Яндекс.Диске.
    - Хранит локальный файл индекса: ./data/kb_index.json
    - Синхронизация: сравнивает файлы на диске и локальный индекс — считает added/updated/deleted/unchanged.
    - list_documents: возвращает KBMeta для меню выбора в /kb.
    """

    def __init__(self, settings):
        self.settings = settings
        self.disk = yadisk.Client(token=settings.yandex_disk_token)
        self.folder = getattr(settings, "yandex_disk_folder", "База Знаний")
        self.data_dir = os.path.abspath("./data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.index_file = os.path.join(self.data_dir, "kb_index.json")

    def _load_local_index(self) -> dict:
        if not os.path.exists(self.index_file):
            return {"files": {}}
        with open(self.index_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_local_index(self, idx: dict) -> None:
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)

    def _remote_listing(self) -> dict[str, dict]:
        """
        Возвращает словарь {id: {path, title, modified, encrypted}}.
        Для простоты используем path как id (стабильно и человекочитаемо).
        """
        result = {}
        try:
            folder_path = f"disk:/{self.folder}"
            res = self.disk.get_meta(folder_path, fields="name,_embedded.items.name,_embedded.items.modified,_embedded.items.path,_embedded.items.type")
            if not res or "_embedded" not in res or "items" not in res["_embedded"]:
                return result
            for it in res["_embedded"]["items"]:
                # Фильтруем только файлы
                if it.get("type") != "file":
                    continue
                path = it.get("path", "")
                name = it.get("name", os.path.basename(path))
                modified = it.get("modified")
                # Я.Диск отдает modified как строку ISO
                updated_at = None
                if modified:
                    try:
                        updated_at = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                    except Exception:
                        updated_at = None
                # Простая эвристика шифрования: по расширению или имени
                encrypted = name.lower().endswith(".pdf") and "encrypted" in name.lower()
                doc_id = path  # используем путь как id
                result[doc_id] = {
                    "id": doc_id,
                    "path": path,
                    "title": name,
                    "updated_at": updated_at.isoformat() if updated_at else None,
                    "encrypted": encrypted,
                }
        except Exception as e:
            logger.exception("KB: remote listing failed: %s", e)
        return result

    def sync(self) -> Tuple[int, int, int, int]:
        """
        Возвращает (added, updated, deleted, unchanged).
        Для минимальной инвазивности — без скачивания и парсинга контента (это можно добавить позже).
        """
        local = self._load_local_index()
        remote = self._remote_listing()

        local_files = local.get("files", {})
        added = updated = deleted = unchanged = 0

        # Найти добавленные/обновленные
        for doc_id, meta in remote.items():
            if doc_id not in local_files:
                added += 1
            else:
                # сравним updated_at
                if local_files[doc_id].get("updated_at") != meta.get("updated_at"):
                    updated += 1
                else:
                    unchanged += 1

        # Найти удаленные
        for doc_id in list(local_files.keys()):
            if doc_id not in remote:
                deleted += 1

        # Обновить локальный индекс
        self._save_local_index({"files": remote})

        logger.info("KB sync: added=%s updated=%s deleted=%s unchanged=%s", added, updated, deleted, unchanged)
        return added, updated, deleted, unchanged

    def list_documents(self) -> List[KBMeta]:
        idx = self._load_local_index()
        files = idx.get("files", {})
        docs: List[KBMeta] = []
        for v in files.values():
            updated = None
            if v.get("updated_at"):
                try:
                    updated = datetime.fromisoformat(v["updated_at"])
                except Exception:
                    updated = None
            docs.append(
                KBMeta(
                    id=v["id"],
                    title=v["title"],
                    path=v["path"],
                    encrypted=bool(v.get("encrypted", False)),
                    updated_at=updated,
                    pages=None,
                )
            )
        # Сортировка: по дате убыв.
        docs.sort(key=lambda d: d.updated_at or datetime.fromtimestamp(0), reverse=True)
        return docs
