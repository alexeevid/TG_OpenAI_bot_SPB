from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.settings import Settings
from app.db.repo_kb import KBRepo
from app.kb.indexer import KbIndexer
from app.kb.parsers import (
    detect_ext,
    is_image_ext,
    parse_csv_bytes,
    parse_docx_bytes,
    parse_image_bytes_best_effort,
    parse_pdf_bytes,
    parse_txt_bytes,
    parse_xlsx_bytes,
)

log = logging.getLogger(__name__)


@dataclass
class ScanReport:
    """Что изменилось на Диске относительно БД (активных документов)."""

    new: List[Dict[str, Any]]
    outdated: List[Dict[str, Any]]
    deleted: List[Dict[str, Any]]


@dataclass
class SyncResult:
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    errors: int = 0


class KbSyncer:
    """
    Синхронизация базы знаний (Яндекс.Диск -> kb_documents + kb_chunks).

    Публичный API ДОЛЖЕН соответствовать handlers/kb.py:
      - scan() -> ScanReport (new/outdated/deleted)
      - sync() -> (ScanReport, ok, fail, deleted_count)
      - status_summary() -> Dict[str, Any]
    """

    def __init__(self, settings: Settings, repo: KBRepo, indexer: KbIndexer, yandex_client: Any):
        self._cfg = settings
        self._repo = repo
        self._indexer = indexer
        self._y = yandex_client

    # -----------------------------
    # helpers
    # -----------------------------
    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value.strip():
            try:
                # Yandex часто дает ISO + "Z"
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return None
        return None

    def _parse_to_text(self, filename: str, data: bytes) -> str:
        ext = detect_ext(filename)

        if ext in ("txt", "md", "log"):
            return parse_txt_bytes(data)

        if ext == "pdf":
            return parse_pdf_bytes(data)

        if ext == "docx":
            return parse_docx_bytes(data)

        if ext in ("xlsx", "xls"):
            return parse_xlsx_bytes(data)

        if ext == "csv":
            return parse_csv_bytes(data)

        if is_image_ext(ext):
            return parse_image_bytes_best_effort(data)

        return ""

    def _disk_files(self) -> List[Dict[str, Any]]:
        """
        Нормализуем метаданные с Диска к единому виду.
        Важно: "modified" (как в клиенте) тоже считаем "modified_at" (наш ключ).
        """
        raw: List[Dict[str, Any]] = self._y.list_kb_files_metadata() or []
        out: List[Dict[str, Any]] = []
        for f in raw:
            path = f.get("path") or f.get("full_path") or ""
            if not path:
                continue

            title = f.get("name") or f.get("title") or path.split("/")[-1]
            resource_id = f.get("resource_id")
            md5 = f.get("md5")
            size = f.get("size")

            # В проде у клиента поле чаще называется "modified"
            modified_at = f.get("modified_at")
            if modified_at is None:
                modified_at = f.get("modified")
            modified_at = self._parse_dt(modified_at)

            out.append(
                {
                    "path": path,
                    "title": title,
                    "resource_id": resource_id,
                    "md5": md5,
                    "size": int(size) if isinstance(size, (int, float)) else (int(size) if str(size).isdigit() else None),
                    "modified_at": modified_at,
                }
            )
        return out

    # -----------------------------
    # public API
    # -----------------------------
    def scan(self) -> ScanReport:
        """
        new:
          - есть на Диске, но нет в БД (по path), либо есть, но is_active=False (считаем как new для админа)
        outdated:
          - есть на Диске и в БД (active), но needs_reindex=True
        deleted:
          - есть в БД (active), но нет на Диске
        """
        disk = self._disk_files()
        disk_by_path = {x["path"]: x for x in disk}

        db_docs = self._repo.list_documents_brief(active_only=True)
        db_by_path = {x["path"]: x for x in db_docs}

        new: List[Dict[str, Any]] = []
        outdated: List[Dict[str, Any]] = []
        deleted: List[Dict[str, Any]] = []

        # deleted (active in DB but missing on disk)
        for p, d in db_by_path.items():
            if p not in disk_by_path:
                deleted.append({"id": d["id"], "path": p})

        # new/outdated based on disk
        for p, f in disk_by_path.items():
            db = db_by_path.get(p)
            if not db:
                new.append({"path": p, "title": f.get("title")})
                continue

            needs = self._repo.document_needs_reindex(
                document_id=int(db["id"]),
                md5=f.get("md5"),
                modified_at=f.get("modified_at"),
                size=f.get("size"),
            )
            if needs:
                outdated.append({"id": int(db["id"]), "path": p, "title": f.get("title")})

        return ScanReport(new=new, outdated=outdated, deleted=deleted)

    def sync(self) -> Tuple[ScanReport, int, int, int]:
        """
        Возвращает:
          (report, ok, fail, deleted_count)
        где ok/fail — количество обработанных файлов (индексация/ошибки),
        deleted_count — сколько активных документов исчезло с Диска.
        """
        report = self.scan()

        ok = 0
        fail = 0

        disk_files = self._disk_files()
        scanned = len(disk_files)

        # Delete-propagation: всё делаем inactive, затем активируем найденные.
        try:
            self._repo.mark_all_documents_inactive()
        except Exception as e:
            log.warning("mark_all_documents_inactive failed (continue): %s", e)

        for f in disk_files:
            path = f["path"]
            title = f.get("title") or path.split("/")[-1]
            resource_id = f.get("resource_id")
            md5 = f.get("md5")
            size = f.get("size")
            modified_at = f.get("modified_at")

            document_id: Optional[int] = None
            try:
                document_id = self._repo.upsert_document(
                    path=path,
                    title=title,
                    resource_id=resource_id,
                    md5=md5,
                    size=size,
                    modified_at=modified_at,
                    is_active=True,
                    status=None,
                    last_error=None,
                )

                needs = self._repo.document_needs_reindex(
                    document_id=document_id,
                    md5=md5,
                    modified_at=modified_at,
                    size=size,
                )
                if not needs:
                    continue

                data: bytes = self._y.download(path)
                text = self._parse_to_text(title, data).strip()

                if not text:
                    self._repo.set_document_status(
                        document_id=document_id,
                        status="skipped",
                        last_error="Empty text after parsing (possibly encrypted PDF or unsupported format).",
                    )
                    continue

                n = self._indexer.reindex_document(document_id=document_id, text=text)
                self._repo.set_document_indexed(document_id=document_id)

                log.info("KB indexed %s chunks for %s", n, path)
                ok += 1

            except Exception as e:
                log.exception("KB sync failed for path=%s: %s", path, e)
                try:
                    if document_id:
                        self._repo.set_document_status(document_id=document_id, status="error", last_error=str(e))
                except Exception:
                    pass
                fail += 1

        # deleted_count — сколько активных ранее исчезло
        deleted_count = len(report.deleted)

        log.info("KB sync finished: scanned=%s ok=%s fail=%s deleted=%s", scanned, ok, fail, deleted_count)
        return report, ok, fail, deleted_count

    def status_summary(self) -> Dict[str, Any]:
        """
        Короткая сводка для /kb status (админ).
        """
        st = self._repo.status_summary()
        # На всякий случай добавим "scan" summary (дешево)
        rep = self.scan()
        st.update(
            {
                "disk_new": len(rep.new),
                "disk_outdated": len(rep.outdated),
                "disk_deleted": len(rep.deleted),
            }
        )
        return st


# stable public API name
KBSyncer = KbSyncer
