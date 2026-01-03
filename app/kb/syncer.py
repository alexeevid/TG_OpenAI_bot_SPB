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


class KBSyncer:
    def __init__(self, yandex_client, embedder, kb_repo, cfg, session_factory):
        self.yd = yandex_client
        self.embedder = embedder
        self.kb_repo = kb_repo
        self.cfg = cfg
        self.sf = session_factory

        # 🔴 КРИТИЧЕСКИ ВАЖНО
        self._ensure_tables_exist()

    # ------------------------------------------------------------------
    # bootstrap
    # ------------------------------------------------------------------
    def _ensure_tables_exist(self) -> None:
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

    # ------------------------------------------------------------------
    # registry helpers
    # ------------------------------------------------------------------
    def _registry_all(self) -> Dict[str, Dict[str, Any]]:
        with self.sf() as s:
            rows = s.execute(sqltext("SELECT * FROM kb_files")).mappings().all()
        return {r["resource_id"]: dict(r) for r in rows}

    def _registry_upsert(self, f: Dict[str, Any]) -> None:
        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    INSERT INTO kb_files
                        (resource_id, path, modified_disk, md5_disk, size_disk, last_checked_at)
                    VALUES
                        (:rid, :path, :mod, :md5, :size, NOW())
                    ON CONFLICT (resource_id)
                    DO UPDATE SET
                        path=EXCLUDED.path,
                        modified_disk=EXCLUDED.modified_disk,
                        md5_disk=EXCLUDED.md5_disk,
                        size_disk=EXCLUDED.size_disk,
                        last_checked_at=NOW()
                    """
                ),
                {
                    "rid": f["resource_id"],
                    "path": f["path"],
                    "mod": f.get("modified_disk"),
                    "md5": f.get("md5_disk"),
                    "size": f.get("size_disk"),
                },
            )
            s.commit()

    def _set_status(self, rid: str, status: str, err: Optional[str] = None):
        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    UPDATE kb_files
                    SET status=:st,
                        last_error=:err,
                        indexed_at=CASE WHEN :st='indexed' THEN NOW() ELSE indexed_at END
                    WHERE resource_id=:rid
                    """
                ),
                {"rid": rid, "st": status, "err": err},
            )
            s.commit()

    # ------------------------------------------------------------------
    # yandex snapshot
    # ------------------------------------------------------------------
    def list_kb_files_metadata(self) -> List[Dict[str, Any]]:
        return list(self.yd.list_kb_files_metadata())

    def scan(self) -> ScanReport:
        snap = self.list_kb_files_metadata()
        db = self._registry_all()

        new, outdated, deleted = [], [], []

        snap_ids = set()

        for f in snap:
            rid = f["resource_id"]
            snap_ids.add(rid)
            old = db.get(rid)

            self._registry_upsert(f)

            if not old:
                new.append(f)
            else:
                if (
                    old["md5_disk"] != f.get("md5_disk")
                    or old["size_disk"] != f.get("size_disk")
                ):
                    outdated.append(f)

        for rid, old in db.items():
            if rid not in snap_ids and old["status"] != "deleted":
                deleted.append(old)
                self._set_status(rid, "deleted")

        for f in new:
            self._set_status(f["resource_id"], "new")
        for f in outdated:
            self._set_status(f["resource_id"], "outdated")

        return ScanReport(new=new, outdated=outdated, deleted=deleted)

    # ------------------------------------------------------------------
    # indexing
    # ------------------------------------------------------------------
    def _parse_to_text(self, path: str, data: bytes) -> str:
        ext = detect_ext(path)
        if ext in {"txt", "md"}:
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
        report = self.scan()
        ok = fail = 0

        for f in report.new + report.outdated:
            try:
                data = self.yd.download(f["path"])
                text = self._parse_to_text(f["path"], data)

                doc_id = self.kb_repo.upsert_document(
                    resource_id=f["resource_id"],
                    path=f["path"],
                )
                self.kb_repo.delete_chunks_by_document_id(doc_id)

                chunks = [text[i:i+900] for i in range(0, len(text), 750)]
                vectors = self.embedder.embed(chunks)

                rows = [(doc_id, i, chunks[i], vectors[i]) for i in range(len(chunks))]
                self.kb_repo.insert_chunks_bulk(rows)

                self._set_status(f["resource_id"], "indexed")
                ok += 1

            except Exception as e:
                self._set_status(f["resource_id"], "error", repr(e))
                fail += 1

        return report, ok, fail, len(report.deleted)
