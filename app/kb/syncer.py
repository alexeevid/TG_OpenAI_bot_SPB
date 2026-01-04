from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List

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
class SyncResult:
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    errors: int = 0


class KbSyncer:
    """
    Синхронизация БЗ (Яндекс.Диск -> kb_documents + kb_chunks).

    ВАЖНО: этот syncer согласован с реальными контрактами текущего KBRepo и YandexDiskClient в вашем архиве:
    - KBRepo.upsert_document(...) -> int (document_id)
    - KBRepo.document_needs_reindex(document_id, md5, modified_at, size)
    - KBRepo.set_document_status(...)
    - KBRepo.set_document_indexed(...)
    - YandexDiskClient.download(path) -> bytes
    """

    def __init__(self, repo: KBRepo, indexer: KbIndexer, yandex_client: Any):
        self._repo = repo
        self._indexer = indexer
        self._y = yandex_client

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

    def run(self) -> SyncResult:
        res = SyncResult()

        files: List[Dict[str, Any]] = self._y.list_kb_files_metadata()
        res.scanned = len(files)

        # delete-propagation: всё делаем inactive, затем активируем найденные
        try:
            self._repo.mark_all_documents_inactive()
        except Exception as e:
            log.warning("mark_all_documents_inactive failed (continue): %s", e)

        for f in files:
            path = f.get("path") or f.get("full_path") or ""
            if not path:
                res.skipped += 1
                continue

            title = f.get("name") or f.get("title") or path.split("/")[-1]
            resource_id = f.get("resource_id")
            md5 = f.get("md5")
            size = f.get("size")
            modified_at = f.get("modified_at")

            # modified_at может быть ISO-string
            if isinstance(modified_at, str):
                try:
                    modified_at = datetime.fromisoformat(modified_at.replace("Z", "+00:00"))
                except Exception:
                    modified_at = None

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
                    res.skipped += 1
                    continue

                # качаем контент (в вашем клиенте метод называется download)
                data: bytes = self._y.download(path)
                text = self._parse_to_text(title, data).strip()

                if not text:
                    # не вводим выдуманные поля. Фиксируем статус и ошибку.
                    self._repo.set_document_status(
                        document_id=document_id,
                        status="skipped",
                        last_error="Empty text after parsing (possibly encrypted PDF or unsupported format).",
                    )
                    res.skipped += 1
                    continue

                n = self._indexer.reindex_document(document_id=document_id, text=text)
                self._repo.set_document_indexed(document_id=document_id)
                res.indexed += 1
                log.info("KB indexed: %s chunks for %s", n, path)

            except Exception as e:
                log.exception("KB sync failed for path=%s: %s", path, e)
                try:
                    # мягко фиксируем ошибку, но не валим весь sync
                    if "document_id" in locals() and document_id:
                        self._repo.set_document_status(
                            document_id=document_id,
                            status="error",
                            last_error=str(e),
                        )
                except Exception:
                    pass
                res.errors += 1

        return res


# стабильное публичное имя (в app.main импортируется KBSyncer)
KBSyncer = KbSyncer
