# bot/knowledge_base/indexer.py
from __future__ import annotations

import io
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import yadisk  # type: ignore
except Exception:
    yadisk = None  # будет видно в логах

logger = logging.getLogger(__name__)


@dataclass
class IndexedDoc:
    """Минимальная карточка документа в индексе."""
    path: str
    etag: Optional[str] = None
    size: Optional[int] = None
    sha256: Optional[str] = None
    mtime: Optional[int] = None  # unixtime


class KnowledgeBaseIndexer:
    """
    Отвечает за:
      - обход папки Yandex.Disk;
      - решение: добавлять/обновлять/пропускать;
      - возврат статистики синка (added, updated, deleted, unchanged);
    Хранилище индекса — простое in-memory + responsibility вызывать внешние сохранения.
    """

    def __init__(self, settings):
        self.settings = settings
        self._root = (getattr(settings, "yandex_root_path", None)
                      or getattr(settings, "yadisk_folder", None)
                      or "disk:/")
        self._token = getattr(settings, "yandex_disk_token", None) or getattr(settings, "yadisk_token", None)
        self._allowed_ext = os.getenv("KB_ALLOWED_EXT", ".pdf,.docx,.txt,.md,.pptx,.xlsx").lower().split(",")

        # Простой внутренний «каталог» уже индексированных документов
        # В реальной системе вы бы тянули это из БД; тут оставляем in-memory
        self._index: Dict[str, IndexedDoc] = {}

        logger.debug("KB Indexer init: root=%r, allowed_ext=%r, token_exists=%s",
                     self._root, self._allowed_ext, bool(self._token))

        if yadisk is None:
            logger.warning("yadisk module is not available; sync will fail.")

    # --------------- Публичные методы ---------------

    def sync(self) -> Tuple[int, int, int, int]:
        """
        Обходит папку на Диске, решает diff c текущим self._index,
        возвращает кортеж (added, updated, deleted, unchanged).
        """
        t0 = time.time()
        logger.info("KB sync started. Root=%s", self._root)

        if not self._token:
            logger.error("KB sync failed: Yandex Disk token is missing")
            raise RuntimeError("Yandex Disk token is missing")

        if yadisk is None:
            raise RuntimeError("yadisk library is not installed")

        y = yadisk.Client(token=self._token)

        # 1) Сканируём дерево
        files = self._scan_disk(y, self._root)

        # 2) Сопоставляем с локальным индексом
        added, updated, deleted, unchanged = self._diff_and_apply(files)

        dt = time.time() - t0
        logger.info(
            "KB sync finished in %.2fs: added=%d, updated=%d, deleted=%d, unchanged=%d",
            dt, added, updated, deleted, unchanged
        )
        return added, updated, deleted, unchanged

    def list_documents(self) -> List[IndexedDoc]:
        """Возвращает список документов из текущего индекса."""
        return list(self._index.values())

    # Утилита: подробный отчёт, что видим на диске и как фильтруем.
    def diagnose(self, max_items: int = 200) -> str:
        lines: List[str] = []
        lines.append(f"KB diagnostics")
        lines.append(f"- Root: {self._root}")
        lines.append(f"- Token exists: {bool(self._token)}")
        lines.append(f"- Allowed EXT: {', '.join(self._allowed_ext)}")
        if yadisk is None:
            lines.append("ERROR: yadisk is not installed.")
            return "\n".join(lines)
        try:
            y = yadisk.Client(token=self._token)
            files = self._scan_disk(y, self._root, log_each=True)
            lines.append(f"- Found files total (after ext filter): {len(files)}")
            for i, doc in enumerate(files[:max_items], 1):
                lines.append(f"{i:03d}. {doc.path} size={doc.size} etag={doc.etag}")
            if len(files) > max_items:
                lines.append(f"... and {len(files) - max_items} more")
        except Exception as e:
            lines.append(f"ERROR during diagnose: {e}")
        return "\n".join(lines)

    # --------------- Внутренние ---------------

    def _scan_disk(self, y, root: str, log_each: bool = False) -> List[IndexedDoc]:
        """
        Рекурсивно обходит папку на Я.Диске и возвращает список файлов,
        прошедших фильтр расширений.
        """
        logger.debug("Scan start: %s", root)
        out: List[IndexedDoc] = []

        def walk(path: str):
            try:
                res = y.get_meta(path, fields="items.name,items.path,items.type,items.size,items.etag,items.modified,limit,offset,_embedded.items")  # type: ignore
            except Exception as e:
                logger.error("get_meta failed for %s: %s", path, e)
                return

            # API Yandex Disk: у папок _embedded.items — список содержимого
            embedded = getattr(res, "_embedded", None)
            items = []
            if embedded and hasattr(embedded, "items"):
                items = embedded.items  # type: ignore

            for it in items:
                it_type = getattr(it, "type", None)
                it_path = getattr(it, "path", None)
                it_name = getattr(it, "name", None)

                if it_type == "dir":
                    walk(it_path)
                    continue

                if it_type == "file":
                    ext = (os.path.splitext(it_name or "")[1] or "").lower()
                    if self._allowed_ext and ext not in self._allowed_ext:
                        if log_each:
                            logger.debug("Skip by ext: %s", it_path)
                        continue

                    size = getattr(it, "size", None)
                    etag = getattr(it, "etag", None)
                    mtime = None
                    try:
                        # modified: '2025-07-29T08:14:03+00:00'
                        mod = getattr(it, "modified", None)
                        if mod:
                            # простая попытка перевести в unixtime
                            # делаем без dateutil, чтобы не добавлять зависимость
                            mtime = int(time.mktime(time.strptime(str(mod)[:19], "%Y-%m-%dT%H:%M:%S")))
                    except Exception:
                        pass

                    doc = IndexedDoc(path=it_path, etag=etag, size=size, mtime=mtime)
                    out.append(doc)
                    if log_each:
                        logger.debug("File candidate: %s size=%s etag=%s", it_path, size, etag)
                else:
                    if log_each:
                        logger.debug("Skip non-file: %s type=%s", it_path, it_type)

        walk(root)
        logger.debug("Scan complete, candidates=%d", len(out))
        return out

    def _diff_and_apply(self, files: List[IndexedDoc]) -> Tuple[int, int, int, int]:
        """
        Очень простой дифф:
          - новый путь → added
          - путь есть, но изменился etag/size/mtime → updated
          - в индексе был, а в дереве нет → deleted
          - остальное → unchanged
        Здесь мы только ведём in-memory self._index (как демонстрация).
        """
        current_paths = {d.path for d in files}
        index_paths = set(self._index.keys())

        to_add = [d for d in files if d.path not in index_paths]
        to_update: List[IndexedDoc] = []
        unchanged = 0

        for d in files:
            if d.path in self._index:
                old = self._index[d.path]
                if (d.etag and d.etag != old.etag) or (d.size is not None and d.size != old.size) or (
                        d.mtime and d.mtime != old.mtime
                ):
                    to_update.append(d)
                else:
                    unchanged += 1

        to_delete = index_paths - current_paths

        logger.debug("Diff result: add=%d, update=%d, delete=%d, unchanged=%d",
                     len(to_add), len(to_update), len(to_delete), unchanged)

        # Применяем
        for d in to_add:
            self._index[d.path] = d
        for d in to_update:
            self._index[d.path] = d
        for p in to_delete:
            self._index.pop(p, None)

        return len(to_add), len(to_update), len(to_delete), unchanged
