# app/kb/syncer.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text as sqltext

from app.kb.parsers import (
    detect_ext,
    is_image_ext,
    parse_csv_bytes,
    parse_docx_bytes,
    parse_image_bytes_best_effort,
    parse_pdf_bytes,
    parse_text_bytes,
    parse_xlsx_bytes,
)


@dataclass
class ScanReport:
    new: List[Dict[str, Any]]
    outdated: List[Dict[str, Any]]
    deleted: List[Dict[str, Any]]


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Yandex often returns ISO with Z
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


class KBSyncer:
    """
    ВАЖНО: сигнатура конструктора сделана ПОД ТВОЙ app/main.py:
        KBSyncer(yd, embedder, kb_repo, cfg, session_factory)

    yandex_client:
      - list_files(...) или аналог (см. list_kb_files_metadata ниже)
      - download(path) -> bytes

    embedder:
      - embed(texts: list[str]) -> list[list[float]]

    kb_repo:
      - upsert_document(path, title)
      - delete_chunks_by_document_id(doc_id)
      - insert_chunks_bulk(...)
    """

    def __init__(self, yandex_client, embedder, kb_repo, cfg, session_factory):
        self.yd = yandex_client
        self.embedder = embedder
        self.kb_repo = kb_repo
        self.cfg = cfg
        self.sf = session_factory

        self._ensure_kb_registry_table()

    # ---------------- registry table (kb_files) ----------------
    def _ensure_kb_registry_table(self) -> None:
        # Без Alembic: создаём таблицу реестра при старте (если нет).
        ddl = """
        CREATE TABLE IF NOT EXISTS kb_files (
            resource_id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            modified_disk TIMESTAMP NULL,
            md5_disk TEXT NULL,
            size_disk BIGINT NULL,
            indexed_at TIMESTAMP NULL,
            status TEXT NOT NULL DEFAULT 'new',
            last_error TEXT NULL,
            last_checked_at TIMESTAMP NULL
        );
        """
        with self.sf() as s:
            s.execute(sqltext(ddl))
            s.commit()

    def _registry_load(self) -> Dict[str, Dict[str, Any]]:
        with self.sf() as s:
            rows = s.execute(
                sqltext(
                    """
                    SELECT resource_id, path, modified_disk, md5_disk, size_disk,
                           indexed_at, status, last_error, last_checked_at
                    FROM kb_files
                    """
                )
            ).all()

        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            out[str(r[0])] = {
                "resource_id": str(r[0]),
                "path": r[1],
                "modified_disk": r[2],
                "md5_disk": r[3],
                "size_disk": r[4],
                "indexed_at": r[5],
                "status": r[6],
                "last_error": r[7],
                "last_checked_at": r[8],
            }
        return out

    def _registry_upsert_snapshot(self, snap: List[Dict[str, Any]]) -> None:
        now = datetime.utcnow()
        with self.sf() as s:
            for f in snap:
                s.execute(
                    sqltext(
                        """
                        INSERT INTO kb_files (resource_id, path, modified_disk, md5_disk, size_disk, last_checked_at)
                        VALUES (:rid, :path, :mod, :md5, :size, :chk)
                        ON CONFLICT (resource_id)
                        DO UPDATE SET
                            path = EXCLUDED.path,
                            modified_disk = EXCLUDED.modified_disk,
                            md5_disk = EXCLUDED.md5_disk,
                            size_disk = EXCLUDED.size_disk,
                            last_checked_at = EXCLUDED.last_checked_at
                        """
                    ),
                    {
                        "rid": f["resource_id"],
                        "path": f["path"],
                        "mod": f.get("modified_disk"),
                        "md5": f.get("md5_disk"),
                        "size": f.get("size_disk"),
                        "chk": now,
                    },
                )
            s.commit()

    def _registry_set_status(self, rid: str, status: str, err: Optional[str] = None) -> None:
        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    UPDATE kb_files
                    SET status=:st,
                        last_error=:err,
                        indexed_at = CASE WHEN :st='indexed' THEN NOW() ELSE indexed_at END
                    WHERE resource_id=:rid
                    """
                ),
                {"st": status, "err": (err or None), "rid": rid},
            )
            s.commit()

    def status_summary(self) -> Dict[str, int]:
        with self.sf() as s:
            rows = s.execute(sqltext("SELECT status, COUNT(*) FROM kb_files GROUP BY status")).all()
        return {str(r[0]): int(r[1]) for r in rows}

    # ---------------- yandex snapshot ----------------
    def list_kb_files_metadata(self) -> List[Dict[str, Any]]:
        """
        Унифицированный слой. Если у твоего клиента уже есть list_kb_files_metadata — используем его.
        Иначе пробуем собрать из list() (плоско) или из существующих методов.
        """
        if hasattr(self.yd, "list_kb_files_metadata"):
            return list(self.yd.list_kb_files_metadata())

        # Fallback: если есть .list(root_path)
        root = getattr(self.cfg, "yandex_root_path", "") or getattr(self.cfg, "YANDEX_ROOT_PATH", "") or ""
        if hasattr(self.yd, "list"):
            items = self.yd.list(root)
            out = []
            for it in items:
                if it.get("type") == "dir":
                    continue
                out.append(
                    {
                        "resource_id": it.get("resource_id") or it.get("md5") or it.get("path"),
                        "path": it.get("path"),
                        "modified_disk": _parse_dt(it.get("modified")),
                        "md5_disk": it.get("md5"),
                        "size_disk": it.get("size"),
                    }
                )
            return [x for x in out if x.get("resource_id") and x.get("path")]

        raise RuntimeError("YandexDiskClient must provide list_kb_files_metadata() or list()")

    def scan(self) -> ScanReport:
        snap = self.list_kb_files_metadata()
        self._registry_upsert_snapshot(snap)

        db = self._registry_load()
        snap_by_id = {f["resource_id"]: f for f in snap}

        new: List[Dict[str, Any]] = []
        outdated: List[Dict[str, Any]] = []
        deleted: List[Dict[str, Any]] = []

        for rid, f in snap_by_id.items():
            old = db.get(rid)
            if not old:
                new.append(f)
                continue
            changed = (
                old.get("md5_disk") != f.get("md5_disk")
                or old.get("modified_disk") != f.get("modified_disk")
                or old.get("size_disk") != f.get("size_disk")
            )
            if changed:
                outdated.append(f)

        for rid, old in db.items():
            if rid not in snap_by_id and old.get("status") != "deleted":
                deleted.append(old)

        # пометим статусы
        for f in new:
            self._registry_set_status(f["resource_id"], "new")
        for f in outdated:
            self._registry_set_status(f["resource_id"], "outdated")
        for f in deleted:
            self._registry_set_status(f["resource_id"], "deleted")

        return ScanReport(new=new, outdated=outdated, deleted=deleted)

    # ---------------- parse + index ----------------
    def _parse_to_text(self, path: str, data: bytes) -> str:
        ext = detect_ext(path)
        if ext in {"txt", "md", "log"}:
            return parse_text_bytes(data)
        if ext == "pdf":
            return parse_pdf_bytes(data)
        if ext == "docx":
            return parse_docx_bytes(data)
        if ext == "csv":
            return parse_csv_bytes(data)
        if ext in {"xlsx", "xlsm"}:
            return parse_xlsx_bytes(data)
        if is_image_ext(ext):
            return parse_image_bytes_best_effort(data)
        return parse_text_bytes(data)

    def sync(self) -> Tuple[ScanReport, int, int, int]:
        """
        Возвращает: (report, indexed_ok, indexed_failed, deleted_marked)
        """
        report = self.scan()

        ok = 0
        fail = 0
        deleted = len(report.deleted)

        # индексируем сначала outdated, потом new
        targets = report.outdated + report.new

        for f in targets:
            path = f["path"]
            rid = f["resource_id"]

            ext = detect_ext(path)
            allowed = getattr(self.cfg, "KB_ALLOWED_EXTS", None)
            if isinstance(allowed, set) and allowed and ext not in allowed:
                continue

            try:
                data = self.yd.download(path)
                text = self._parse_to_text(path, data)

                # 1) документ
                title = None
                if hasattr(self.kb_repo, "upsert_document"):
                    doc_id = self.kb_repo.upsert_document(path=path, title=title)
                else:
                    raise RuntimeError("KBRepo missing upsert_document()")

                # 2) пересоздать чанки
                if hasattr(self.kb_repo, "delete_chunks_by_document_id"):
                    self.kb_repo.delete_chunks_by_document_id(doc_id)
                else:
                    # если нет — не фейлим, просто продолжаем
                    pass

                # 3) нарезка и эмбеддинги
                chunk_size = int(getattr(self.cfg, "CHUNK_SIZE", 900))
                overlap = int(getattr(self.cfg, "CHUNK_OVERLAP", 150))
                step = max(1, chunk_size - overlap)

                chunks: List[Tuple[int, str]] = []
                i = 0
                order = 0
                while i < len(text):
                    part = text[i : i + chunk_size]
                    if part.strip():
                        chunks.append((order, part))
                        order += 1
                    i += step

                if chunks:
                    vectors = self.embedder.embed([t for _, t in chunks])
                    rows = [(doc_id, o, t, vectors[idx]) for idx, (o, t) in enumerate(chunks)]
                    self.kb_repo.insert_chunks_bulk(rows)

                self._registry_set_status(rid, "indexed", err=None)
                ok += 1

            except Exception as e:
                self._registry_set_status(rid, "error", err=repr(e))
                fail += 1

        return report, ok, fail, deleted
