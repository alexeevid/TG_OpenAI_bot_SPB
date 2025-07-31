# bot/knowledge_base/indexer.py
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import yadisk  # опционально
except Exception:  # pragma: no cover
    yadisk = None


@dataclass
class KBDocument:
    doc_id: str          # стабильный идентификатор (у нас это remote path, например "disk:/База Знаний/PMBOK.pdf")
    title: str
    mime: Optional[str] = None
    size: Optional[int] = None
    updated_at: Optional[str] = None  # ISO строка
    etag: Optional[str] = None        # хеш/etag с диска, если доступен
    is_encrypted: bool = False        # если нужен пароль при чтении (на будущее)
    note: Optional[str] = None        # любая пометка


class KnowledgeBaseIndexer:
    """
    Поддерживает РЕЕСТР документов БЗ (без привязки к конкретному векторному бэкенду).
    Источник — Яндекс.Диск (если настроен), либо локальная папка.

    Реестр хранится в data/kb_registry.json:
    {
      "docs": { "<doc_id>": {...}, ... },
      "last_sync": "2025-07-30T10:15:00Z"
    }
    """

    def __init__(self, settings, registry_path: str = "data/kb_registry.json"):
        self.settings = settings
        self.registry_path = registry_path
        os.makedirs(os.path.dirname(self.registry_path), exist_ok=True)

        # Настройки источника БЗ
        self.kb_root = getattr(settings, "kb_root", "disk:/База Знаний")  # для Yandex.Disk
        self.local_kb_dir = getattr(settings, "kb_local_dir", None)       # если хотите локальную папку вместо YD

        # Yandex Disk
        self.ya_token = getattr(settings, "yadisk_token", None)
        self._yadisk = None
        if self.ya_token and yadisk:
            try:
                self._yadisk = yadisk.YaDisk(token=self.ya_token)
                logger.info("KB Indexer: Yandex.Disk client initialized")
            except Exception as e:
                logger.warning("KB Indexer: Yandex.Disk init failed: %s", e)

        self._registry = {"docs": {}, "last_sync": None}
        self._load_registry()

    # ---------- Registry I/O ----------

    def _load_registry(self) -> None:
        if os.path.exists(self.registry_path):
            try:
                with open(self.registry_path, "r", encoding="utf-8") as f:
                    self._registry = json.load(f)
            except Exception as e:
                logger.warning("KB Indexer: Failed to read registry, recreating: %s", e)
                self._registry = {"docs": {}, "last_sync": None}

    def _save_registry(self) -> None:
        tmp = {"docs": self._registry.get("docs", {}), "last_sync": self._registry.get("last_sync")}
        with open(self.registry_path, "w", encoding="utf-8") as f:
            json.dump(tmp, f, ensure_ascii=False, indent=2)

    def _put_doc(self, doc: KBDocument) -> None:
        self._registry["docs"][doc.doc_id] = asdict(doc)

    def _del_doc(self, doc_id: str) -> None:
        self._registry["docs"].pop(doc_id, None)

    # ---------- Public: list ----------

    def list_all(self) -> List[KBDocument]:
        return [KBDocument(**d) for d in self._registry.get("docs", {}).values()]

    def get(self, doc_id: str) -> Optional[KBDocument]:
        raw = self._registry.get("docs", {}).get(doc_id)
        return KBDocument(**raw) if raw else None

    # ---------- Public: sync ----------

    def sync(self) -> Tuple[int, int, int, int]:
        """
        Синхронизация реестра:
        - для Yandex.Disk: забираем список из каталога kb_root (без скачивания файлов)
        - для локальной папки: обходим файлы
        Возвращаем (added, updated, deleted, unchanged).
        """
        prev_ids = set(self._registry.get("docs", {}).keys())
        curr_map: Dict[str, KBDocument] = {}

        if self.local_kb_dir and os.path.isdir(self.local_kb_dir):
            added, updated, deleted, unchanged = self._sync_from_local(curr_map)
        elif self._yadisk and self.kb_root.startswith("disk:"):
            added, updated, deleted, unchanged = self._sync_from_yadisk(curr_map)
        else:
            logger.warning("KB Indexer: No source configured (neither local_kb_dir nor Yandex.Disk).")
            added = updated = deleted = unchanged = 0

        # Сохраняем
        self._registry["last_sync"] = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        self._save_registry()
        return added, updated, deleted, unchanged

    # ---------- Helpers ----------

    def _sync_from_local(self, curr_map: Dict[str, KBDocument]) -> Tuple[int, int, int, int]:
        prev = self._registry.get("docs", {})
        for root, _, files in os.walk(self.local_kb_dir):
            for name in files:
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                except Exception:
                    continue
                doc_id = f"file://{os.path.abspath(path)}"
                mime = self._guess_mime(name)
                title = name
                updated = dt.datetime.utcfromtimestamp(st.st_mtime).replace(microsecond=0).isoformat() + "Z"
                curr_map[doc_id] = KBDocument(
                    doc_id=doc_id, title=title, mime=mime, size=st.st_size, updated_at=updated, etag=str(st.st_mtime_ns)
                )

        return self._merge_registry(curr_map, prev)

    def _sync_from_yadisk(self, curr_map: Dict[str, KBDocument]) -> Tuple[int, int, int, int]:
        prev = self._registry.get("docs", {})

        def _walk_folder(folder_path: str):
            try:
                res = self._yadisk.get_meta(folder_path, limit=10000)
            except Exception as e:
                logger.error("KB Indexer: YD get_meta failed for %s: %s", folder_path, e)
                return
            for item in res.items:
                if item.type == "dir":
                    _walk_folder(item.path)
                else:
                    doc_id = item.path  # "disk:/База Знаний/PMBOK.pdf"
                    title = item.name
                    mime = getattr(item, "mime_type", None)
                    size = item.size
                    updated = getattr(item, "modified", None)
                    etag = getattr(item, "md5", None) or getattr(item, "etag", None)
                    curr_map[doc_id] = KBDocument(
                        doc_id=doc_id,
                        title=title,
                        mime=mime,
                        size=size,
                        updated_at=updated,
                        etag=etag,
                    )

        _walk_folder(self.kb_root)
        return self._merge_registry(curr_map, prev)

    def _merge_registry(self, curr_map: Dict[str, KBDocument], prev: Dict[str, dict]) -> Tuple[int, int, int, int]:
        added = updated = unchanged = 0

        # добавленные/обновлённые
        for doc_id, doc in curr_map.items():
            old = prev.get(doc_id)
            if not old:
                self._put_doc(doc)
                added += 1
            else:
                # сравниваем по etag/size/updated_at
                if (doc.etag and doc.etag != old.get("etag")) or (doc.size != old.get("size")) or (doc.updated_at != old.get("updated_at")):
                    self._put_doc(doc)
                    updated += 1
                else:
                    # переносим как есть
                    self._put_doc(KBDocument(**old))
                    unchanged += 1

        # удалённые
        deleted_ids = set(prev.keys()) - set(curr_map.keys())
        for d in deleted_ids:
            self._del_doc(d)

        return added, updated, len(deleted_ids), unchanged

    @staticmethod
    def _guess_mime(filename: str) -> str:
        fn = filename.lower()
        if fn.endswith(".pdf"):
            return "application/pdf"
        if fn.endswith(".docx"):
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if fn.endswith(".xlsx"):
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if fn.endswith(".txt") or fn.endswith(".md"):
            return "text/plain"
        return "application/octet-stream"
