from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from app.kb.registry import KbFileMeta, KbRegistry
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
from app.kb.indexer import KbIndexer
from app.settings import settings


@dataclass
class ScanReport:
    new: List[KbFileMeta]
    outdated: List[KbFileMeta]
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


class KbSyncer:
    """
    yandex_client должен уметь:
      list_kb_files_metadata() -> List[dict{resource_id,path,modified,md5,size}]
      download(path) -> bytes
    """

    def __init__(self, yandex_client, db, openai_client):
        self._yandex = yandex_client
        self._db = db
        self._registry = KbRegistry(db)
        self._indexer = KbIndexer(db=db, openai_client=openai_client)

    def _snapshot(self) -> List[KbFileMeta]:
        raw = self._yandex.list_kb_files_metadata()
        out: List[KbFileMeta] = []
        for r in raw:
            rid = r.get("resource_id")
            path = r.get("path")
            if not rid or not path:
                continue
            out.append(
                KbFileMeta(
                    resource_id=rid,
                    path=path,
                    modified=_parse_yadisk_dt(r.get("modified")),
                    md5=r.get("md5"),
                    size=r.get("size"),
                )
            )
        return out

    def scan(self) -> ScanReport:
        snapshot = self._snapshot()
        self._registry.upsert_snapshot(snapshot)
        new_files, outdated_files, deleted_records = self._registry.reconcile(snapshot)
        deleted_ids = [r["resource_id"] for r in deleted_records]
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
            # В базовой конфигурации без vision/ocr — placeholder.
            return parse_image_bytes_best_effort(data)

        # fallback: пробуем как текст
        return parse_text_bytes(data)

    def sync(self) -> Tuple[ScanReport, int, int, int]:
        """
        Returns: (report, indexed_ok, indexed_failed, deleted_purged)
        """
        report = self.scan()

        # purge deleted
        deleted_purged = 0
        for rid in report.deleted_resource_ids:
            try:
                self._indexer.delete_file_embeddings(rid)
                deleted_purged += 1
            except Exception:
                # не фейлим всю синхронизацию
                pass

        ok = 0
        fail = 0

        # индексируем outdated приоритетнее, потом new
        for f in (report.outdated + report.new):
            # фильтр по разрешённым расширениям
            ext = detect_ext(f.path)
            if settings.KB_ALLOWED_EXTS and ext not in settings.KB_ALLOWED_EXTS:
                continue

            try:
                data = self._yandex.download(f.path)

                if is_image_ext(ext) and settings.KB_ENABLE_OPENAI_VISION:
                    # Vision path (опционально) через OpenAI Responses API :contentReference[oaicite:7]{index=7}
                    cnt = self._indexer.index_image(f.resource_id, f.path, data)
                else:
                    text = self._parse_to_text(f.path, data)
                    cnt = self._indexer.index_document_text(f.resource_id, f.path, text)

                self._registry.mark_indexed(f.resource_id)
                ok += 1
            except Exception as e:
                self._registry.mark_error(f.resource_id, repr(e))
                fail += 1

        return report, ok, fail, deleted_purged

    def reindex_one(self, key: str) -> bool:
        rec = self._registry.get_by_path_or_id(key)
        if not rec:
            return False
        rid = rec["resource_id"]
        path = rec["path"]
        ext = detect_ext(path)
        if settings.KB_ALLOWED_EXTS and ext not in settings.KB_ALLOWED_EXTS:
            return False
        try:
            data = self._yandex.download(path)
            if is_image_ext(ext) and settings.KB_ENABLE_OPENAI_VISION:
                self._indexer.index_image(rid, path, data)
            else:
                text = self._parse_to_text(path, data)
                self._indexer.index_document_text(rid, path, text)

            self._registry.mark_indexed(rid)
            return True
        except Exception as e:
            self._registry.mark_error(rid, repr(e))
            return False

    def status_summary(self) -> dict:
        return self._registry.status_summary()
