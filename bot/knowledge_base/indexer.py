# bot/knowledge_base/indexer.py
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Iterable, List, Tuple

try:
    import yadisk  # установлен в requirements
except Exception:
    yadisk = None  # работаем без YaDisk, если его нет

logger = logging.getLogger(__name__)

DEFAULT_INDEX_DIR = "/data/kb"  # куда кладём manifest.json если не задано иное


@dataclass
class ManifestDoc:
    path: str
    name: str
    size: int
    mtime: float


class KnowledgeBaseIndexer:
    """
    Простая реализация индексатора:
      - при sync() перечисляет документы в корне Я.Диска (или локально) и пишет manifest.json
      - возвращает счётчики added/updated/deleted/unchanged
    Не занимается эмбеддингами — это обязанность Retriever/ingestor.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        # где хранить манифест
        self.index_dir = getattr(settings, "kb_index_dir", None) or DEFAULT_INDEX_DIR
        os.makedirs(self.index_dir, exist_ok=True)
        self.manifest_path = os.path.join(self.index_dir, "manifest.json")

        # параметры Я.Диска
        self.yadisk_token = getattr(settings, "yandex_disk_token", None)
        self.yadisk_root = getattr(settings, "yandex_root_path", "/База Знаний")

        # допустимые расширения
        self.allowed_ext = {".pdf", ".docx", ".txt", ".md", ".pptx"}

    # ---------- публичный API ----------
    def sync(self) -> Tuple[int, int, int, int]:
        """
        Сканирует источник (Я.Диск / локальный каталог) и обновляет manifest.json.
        Возвращает: added, updated, deleted, unchanged
        """
        prev = self._load_manifest()
        prev_by_path = {d["path"]: d for d in prev}

        current = list(self._scan_documents())
        cur_by_path = {d.path: d for d in current}

        added = 0
        updated = 0
        deleted = 0
        unchanged = 0

        # сравнение
        for p, cur in cur_by_path.items():
            if p not in prev_by_path:
                added += 1
            else:
                old = prev_by_path[p]
                if int(old.get("size", 0)) != cur.size or float(old.get("mtime", 0)) != cur.mtime:
                    updated += 1
                else:
                    unchanged += 1

        for p in prev_by_path.keys():
            if p not in cur_by_path:
                deleted += 1

        # сохраняем манифест
        self._save_manifest([asdict(d) for d in current])

        logger.info("KB sync: added=%s updated=%s deleted=%s unchanged=%s",
                    added, updated, deleted, unchanged)
        return added, updated, deleted, unchanged

    def list_manifest_docs(self) -> List[str]:
        """Возвращает список имён документов из manifest.json (для UI)."""
        data = self._load_manifest()
        return [d.get("name") or d.get("path") for d in data]

    # ---------- внутренняя логика ----------
    def _load_manifest(self) -> List[dict]:
        if not os.path.exists(self.manifest_path):
            return []
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                return json.load(f).get("documents", [])
        except Exception as e:
            logger.warning("Cannot read manifest: %s", e)
            return []

    def _save_manifest(self, docs: List[dict]) -> None:
        tmp = self.manifest_path + ".tmp"
        payload = {"updated_at": time.time(), "documents": docs}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.manifest_path)

    def _scan_documents(self) -> Iterable[ManifestDoc]:
        """
        Возвращает список ManifestDoc из источника.
        Если доступен yadisk и есть токен — сканируем папку на диске.
        Иначе смотрим локальную папку self.yadisk_root (если это путь) и берём файлы оттуда.
        """
        if yadisk and self.yadisk_token:
            try:
                ya = yadisk.YaDisk(token=self.yadisk_token)
                # рекурсивный обход файлов в self.yadisk_root
                for item in ya.listdir(self.yadisk_root):
                    yield from self._flatten_yadisk(ya, item)
                return
            except Exception as e:
                logger.warning("YaDisk scan failed, fallback to local: %s", e)

        # fallback: локальный путь
        local_root = self.yadisk_root if os.path.isdir(self.yadisk_root) else self.index_dir
        for root, _, files in os.walk(local_root):
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in self.allowed_ext:
                    continue
                path = os.path.join(root, fn)
                try:
                    st = os.stat(path)
                    yield ManifestDoc(path=path, name=fn, size=st.st_size, mtime=st.st_mtime)
                except FileNotFoundError:
                    continue

    def _flatten_yadisk(self, ya, item) -> Iterable[ManifestDoc]:
        """
        Рекурсивно обходим папки Я.Диска; отдаём только поддерживаемые документы.
        """
        if item["type"] == "dir":
            for sub in ya.listdir(item["path"]):
                yield from self._flatten_yadisk(ya, sub)
            return
        # файл
        name = item["name"]
        ext = os.path.splitext(name)[1].lower()
        if ext not in self.allowed_ext:
            return
        size = int(item.get("size", 0))
        # у Я.Диска 'modified' может быть строкой времени; приводим к ts
        mtime = item.get("modified")
        if isinstance(mtime, str):
            try:
                # 2024-06-10T12:34:56+00:00
                from datetime import datetime
                mtime = datetime.fromisoformat(mtime.replace("Z", "+00:00")).timestamp()
            except Exception:
                mtime = time.time()
        elif not isinstance(mtime, (int, float)):
            mtime = time.time()

        yield ManifestDoc(path=item["path"], name=name, size=size, mtime=float(mtime))
