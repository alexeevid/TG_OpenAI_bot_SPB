from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

from ..clients.yandex_disk_client import YandexDiskClient
from ..db.repo_kb import KBRepo
from ..db.models import KBFile
from ..kb.embedder import Embedder
from ..kb.parsers import (
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
class FileMeta:
    resource_id: str
    path: str
    modified: datetime
    md5: Optional[str]
    size: Optional[int]


@dataclass
class ScanReport:
    new: List[FileMeta]
    outdated: List[FileMeta]
    deleted_resource_ids: List[str]


def _parse_yadisk_dt(s: Optional[str]) -> datetime:
    if not s:
        return datetime.utcfromtimestamp(0)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return datetime.utcfromtimestamp(0)


class KBSyncer:
    def __init__(self, yandex: YandexDiskClient, embedder: Embedder, kb_repo: KBRepo, cfg, session_factory):
        self._yd = yandex
        self._embedder = embedder
        self._repo = kb_repo
        self._cfg = cfg
        self._sf = session_factory

    def _snapshot(self) -> List[FileMeta]:
        raw = self._yd.list_kb_files_metadata()
        out: List[FileMeta] = []
        for r in raw:
            rid = r.get("resource_id")
            path = r.get("path")
            if not rid or not path:
                continue
            out.append(
                FileMeta(
                    resource_id=str(rid),
                    path=str(path),
                    modified=_parse_yadisk_dt(r.get("modified")),
                    md5=r.get("md5"),
                    size=r.get("size"),
                )
            )
        return out

    def _upsert_registry(self, snapshot: List[FileMeta]) -> None:
        now = datetime.utcnow()
        with self._sf() as s:  # type: Session
            for f in snapshot:
                rec = s.query(KBFile).filter_by(resource_id=f.resource_id).first()
                if not rec:
                    rec = KBFile(
                        resource_id=f.resource_id,
                        path=f.path,
                        modified_disk=f.modified,
                        md5_disk=f.md5,
                        size_disk=f.size,
                        status="new",
                        last_checked_at=now,
                    )
                    s.add(rec)
                else:
                    rec.path = f.path
                    rec.modified_disk = f.modified
                    rec.md5_disk = f.md5
                    rec.size_disk = f.size
                    rec.last_checked_at = now
                    s.add(rec)
            s.commit()

    def _reconcile(self, snapshot: List[FileMeta]) -> Tuple[List[FileMeta], List[FileMeta], List[str]]:
        snap_by_id = {f.resource_id: f for f in snapshot}

        with self._sf() as s:
            db_all = s.query(KBFile).all()

        new_files: List[FileMeta] = []
        outdated_files: List[FileMeta] = []
        deleted_ids: List[str] = []

        db_by_id: Dict[str, KBFile] = {r.resource_id: r for r in db_all}

        for rid, f in snap_by_id.items():
            db_row = db_by_id.get(rid)
            if not db_row:
                new_files.append(f)
                continue
            changed = (db_row.md5_disk != f.md5) or (db_row.modified_disk != f.modified) or (db_row.size_disk != f.size)
            if changed:
                outdated_files.append(f)

        for rid, db_row in db_by_id.items():
            if rid not in snap_by_id and db_row.status != "deleted":
                deleted_ids.append(rid)

        with self._sf() as s:
            for f in new_files:
                s.execute(sqltext("UPDATE kb_files SET status='new' WHERE resource_id=:r"), {"r": f.resource_id})
            for f in outdated_files:
                s.execute(sqltext("UPDATE kb_files SET status='outdated' WHERE resource_id=:r"), {"r": f.resource_id})
            for rid in deleted_ids:
                s.execute(sqltext("UPDATE kb_files SET status='deleted' WHERE resource_id=:r"), {"r": rid})
            s.commit()

        return new_files, outdated_files, deleted_ids

    def scan(self) -> ScanReport:
        snap = self._snapshot()
        self._upsert_registry(snap)
        new_files, outdated_files, deleted_ids = self._reconcile(snap)
        return ScanReport(new=new_files, outdated=outdated_files, deleted_resource_ids=deleted_ids)

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

    def _allowed(self, path: str) -> bool:
        exts = getattr(self._cfg, "kb_allowed_exts", None)
        if not exts:
            return True
        ext = detect_ext(path)
        return ext in exts

    def _index_one(self, f: FileMeta) -> None:
        data = self._yd.download(f.path)
        text = self._parse_to_text(f.path, data)

        chunk_size = int(getattr(self._cfg, "chunk_size", 900))
        overlap = int(getattr(self._cfg, "chunk_overlap", 150))
        chunks = []
        i = 0
        order = 0
        n = len(text)
        step = max(1, chunk_size - overlap)
        while i < n:
            part = text[i : i + chunk_size]
            if part.strip():
                chunks.append((order, part))
                order += 1
            i += step

        texts = [c[1] for c in chunks]
        vectors = self._embedder.embed(texts) if texts else []

        doc_id = self._repo.upsert_document(resource_id=f.resource_id, path=f.path, title=f.path)
        self._repo.delete_chunks_by_document_id(doc_id)
        rows = [(doc_id, chunks[idx][0], chunks[idx][1], vectors[idx]) for idx in range(len(chunks))]
        if rows:
            self._repo.insert_chunks_bulk(rows)

    def sync(self) -> Tuple[ScanReport, int, int, int]:
        report = self.scan()

        purged = 0
        for rid in report.deleted_resource_ids:
            try:
                self._repo.delete_chunks_by_resource_id(rid)
                purged += 1
            except Exception:
                pass

        ok = 0
        fail = 0

        for f in (report.outdated + report.new):
            if not self._allowed(f.path):
                continue
            try:
                self._index_one(f)
                ok += 1
                with self._sf() as s:
                    s.execute(
                        sqltext("UPDATE kb_files SET status='indexed', indexed_at=NOW(), last_error=NULL WHERE resource_id=:r"),
                        {"r": f.resource_id},
                    )
                    s.commit()
            except Exception as e:
                fail += 1
                with self._sf() as s:
                    s.execute(
                        sqltext("UPDATE kb_files SET status='error', last_error=:e WHERE resource_id=:r"),
                        {"r": f.resource_id, "e": repr(e)[:2000]},
                    )
                    s.commit()

        return report, ok, fail, purged

    def status_summary(self) -> Dict[str, int]:
        with self._sf() as s:
            rows = s.execute(sqltext("SELECT status, COUNT(*) FROM kb_files GROUP BY status")).fetchall()
        return {str(a): int(b) for (a, b) in rows}

    def reindex_one(self, key: str) -> bool:
        key = (key or "").strip()
        if not key:
            return False
        with self._sf() as s:
            rec = s.query(KBFile).filter((KBFile.resource_id == key) | (KBFile.path == key)).first()
        if not rec:
            return False
        f = FileMeta(resource_id=rec.resource_id, path=rec.path, modified=rec.modified_disk or datetime.utcfromtimestamp(0), md5=rec.md5_disk, size=rec.size_disk)
        try:
            self._index_one(f)
            with self._sf() as s:
                s.execute(sqltext("UPDATE kb_files SET status='indexed', indexed_at=NOW(), last_error=NULL WHERE resource_id=:r"), {"r": rec.resource_id})
                s.commit()
            return True
        except Exception as e:
            with self._sf() as s:
                s.execute(sqltext("UPDATE kb_files SET status='error', last_error=:e WHERE resource_id=:r"), {"r": rec.resource_id, "e": repr(e)[:2000]})
                s.commit()
            return False
