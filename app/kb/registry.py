from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class KbFileMeta:
    resource_id: str
    path: str
    modified: datetime
    md5: Optional[str]
    size: Optional[int]


class KbFileStatus:
    NEW = "new"
    OUTDATED = "outdated"
    INDEXED = "indexed"
    ERROR = "error"
    DELETED = "deleted"
    EXCLUDED = "excluded"


class KbRegistry:
    """
    Реестр KB-файлов (таблица kb_files). Используется для:
    - проверки, что файлы обновились (modified/md5/size)
    - определения new/outdated/deleted
    - контроля ошибок индексации
    """

    def __init__(self, db):
        self._db = db

    def load_all(self) -> Dict[str, dict]:
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT resource_id, path, modified_disk, md5_disk, size_disk,
                       indexed_at, status, last_error, last_checked_at
                FROM kb_files
                """
            )
            rows = cur.fetchall()

        out: Dict[str, dict] = {}
        for r in rows:
            out[r[0]] = {
                "resource_id": r[0],
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

    def upsert_snapshot(self, snapshot: Iterable[KbFileMeta]) -> None:
        now = datetime.utcnow()
        with self._db.cursor() as cur:
            for f in snapshot:
                cur.execute(
                    """
                    INSERT INTO kb_files (resource_id, path, modified_disk, md5_disk, size_disk, status, last_checked_at)
                    VALUES (%s, %s, %s, %s, %s,
                            COALESCE((SELECT status FROM kb_files WHERE resource_id=%s), %s),
                            %s)
                    ON CONFLICT (resource_id)
                    DO UPDATE SET
                        path = EXCLUDED.path,
                        modified_disk = EXCLUDED.modified_disk,
                        md5_disk = EXCLUDED.md5_disk,
                        size_disk = EXCLUDED.size_disk,
                        last_checked_at = EXCLUDED.last_checked_at
                    """,
                    (f.resource_id, f.path, f.modified, f.md5, f.size, f.resource_id, KbFileStatus.NEW, now),
                )
        self._db.commit()

    def reconcile(self, snapshot: Iterable[KbFileMeta]) -> Tuple[List[KbFileMeta], List[KbFileMeta], List[dict]]:
        snap_list = list(snapshot)
        snap_by_id = {f.resource_id: f for f in snap_list}
        db_by_id = self.load_all()

        new_files: List[KbFileMeta] = []
        outdated_files: List[KbFileMeta] = []
        deleted_records: List[dict] = []

        for rid, f in snap_by_id.items():
            db_row = db_by_id.get(rid)
            if not db_row:
                new_files.append(f)
                continue

            changed = (
                (db_row.get("md5_disk") != f.md5)
                or (db_row.get("modified_disk") != f.modified)
                or (db_row.get("size_disk") != f.size)
            )
            if changed:
                outdated_files.append(f)

        for rid, db_row in db_by_id.items():
            if rid not in snap_by_id and db_row.get("status") != KbFileStatus.DELETED:
                deleted_records.append(db_row)

        with self._db.cursor() as cur:
            for f in new_files:
                cur.execute("UPDATE kb_files SET status=%s WHERE resource_id=%s", (KbFileStatus.NEW, f.resource_id))
            for f in outdated_files:
                cur.execute("UPDATE kb_files SET status=%s WHERE resource_id=%s", (KbFileStatus.OUTDATED, f.resource_id))
            for r in deleted_records:
                cur.execute("UPDATE kb_files SET status=%s WHERE resource_id=%s", (KbFileStatus.DELETED, r["resource_id"]))
        self._db.commit()

        return new_files, outdated_files, deleted_records

    def mark_indexed(self, resource_id: str) -> None:
        now = datetime.utcnow()
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE kb_files SET status=%s, indexed_at=%s, last_error=NULL WHERE resource_id=%s",
                (KbFileStatus.INDEXED, now, resource_id),
            )
        self._db.commit()

    def mark_error(self, resource_id: str, error_text: str) -> None:
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE kb_files SET status=%s, last_error=%s WHERE resource_id=%s",
                (KbFileStatus.ERROR, (error_text or "")[:2000], resource_id),
            )
        self._db.commit()

    def status_summary(self) -> Dict[str, int]:
        with self._db.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM kb_files GROUP BY status")
            rows = cur.fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def get_by_path_or_id(self, key: str) -> Optional[dict]:
        with self._db.cursor() as cur:
            cur.execute(
                """
                SELECT resource_id, path, modified_disk, md5_disk, size_disk,
                       indexed_at, status, last_error, last_checked_at
                FROM kb_files
                WHERE resource_id=%s OR path=%s
                LIMIT 1
                """,
                (key, key),
            )
            r = cur.fetchone()
        if not r:
            return None
        return {
            "resource_id": r[0],
            "path": r[1],
            "modified_disk": r[2],
            "md5_disk": r[3],
            "size_disk": r[4],
            "indexed_at": r[5],
            "status": r[6],
            "last_error": r[7],
            "last_checked_at": r[8],
        }
