from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

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
    parse_text_bytes,
    parse_xlsx_bytes,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncResult:
    scanned: int
    indexed: int
    skipped: int
    errors: int


class KbSyncer:
    """Synchronize KB documents from Yandex.Disk into Postgres.

    Design intent:
    - metadata is stored in kb_documents
    - chunks+embeddings are stored in kb_chunks (pgvector)
    - encrypted PDFs are not indexed by batch sync (require password, handled separately)
    """

    def __init__(self, yandex_client, db, openai_client, cfg: Optional[Settings] = None):
        self._y = yandex_client
        self._cfg = cfg or Settings()
        # KBRepo expects session factory + dim
        self._repo = KBRepo(db, dim=self._cfg.embedding_dim)

        from app.kb.embedder import Embedder
        embedder = Embedder(openai_client=openai_client, model=self._cfg.openai_embedding_model)
        self._indexer = KbIndexer(
            kb_repo=self._repo,
            embedder=embedder,
            chunk_size=self._cfg.chunk_size,
            overlap=self._cfg.chunk_overlap,
        )

    def sync(self) -> Dict[str, Any]:
        files = self._y.list_kb_files_metadata()
        scanned = len(files)
        indexed = 0
        skipped = 0
        errors = 0

        # Mark all documents as inactive first; reactivate on scan (delete propagation)
        try:
            self._repo.mark_all_documents_inactive()
        except Exception:
            # if table empty/no permissions, do not fail hard
            pass

        for f in files:
            try:
                rid = (f.get("resource_id") or "").strip()
                path = (f.get("path") or "").strip()
                if not path:
                    continue

                title = path.rsplit("/", 1)[-1]
                md5 = f.get("md5")
                size = f.get("size")
                modified = f.get("modified")
                modified_at = None
                if isinstance(modified, str):
                    # ISO 8601 (Yandex) e.g. 2025-01-01T10:20:30+00:00
                    try:
                        modified_at = datetime.fromisoformat(modified.replace("Z", "+00:00")).replace(tzinfo=None)
                    except Exception:
                        modified_at = None

                # upsert metadata and reactivate
                doc_id = self._repo.upsert_document(
                    path=path,
                    title=title,
                    resource_id=rid or None,
                    md5=md5,
                    size=size,
                    modified_at=modified_at,
                    is_active=True,
                    status="new",
                    last_error=None,
                )

                # Determine if we need reindex
                if not self._repo.document_needs_reindex(doc_id, md5=md5, modified_at=modified_at, size=size):
                    skipped += 1
                    self._repo.set_document_status(doc_id, status="indexed", last_error=None)
                    continue

                ext = detect_ext(path)
                blob = self._y.download(path)

                if not blob:
                    errors += 1
                    self._repo.set_document_status(doc_id, status="error", last_error="empty_download")
                    continue

                # Parse content
                text: str = ""
                if ext in (".txt", ".md"):
                    text = parse_text_bytes(blob)
                elif ext in (".docx",):
                    text = parse_docx_bytes(blob)
                elif ext in (".xlsx",):
                    text = parse_xlsx_bytes(blob)
                elif ext in (".pptx",):
                    text = parse_pptx_bytes(blob)
                elif ext in (".csv",):
                    text = parse_csv_bytes(blob)
                elif ext in (".pdf",):
                    parsed = parse_pdf_bytes(blob, password=None)
                    if parsed.get("needs_password"):
                        errors += 1
                        self._repo.set_document_status(doc_id, status="error", last_error="pdf_password_required")
                        continue
                    text = parsed.get("text") or ""
                elif is_image_ext(ext):
                    text = parse_image_bytes_best_effort(blob)
                else:
                    # unknown type
                    skipped += 1
                    self._repo.set_document_status(doc_id, status="skipped", last_error=f"unsupported_ext:{ext}")
                    continue

                if not text.strip():
                    skipped += 1
                    self._repo.set_document_status(doc_id, status="skipped", last_error="empty_text")
                    continue

                cnt = self._indexer.reindex_document(doc_id, text)
                indexed += 1 if cnt > 0 else 0
                self._repo.set_document_indexed(doc_id)

            except Exception as e:
                errors += 1
                log.exception("KB sync error: %s", e)
                try:
                    # best-effort: mark last processed doc as error
                    if "doc_id" in locals():
                        self._repo.set_document_status(int(doc_id), status="error", last_error=str(e)[:1500])
                except Exception:
                    pass

        return SyncResult(scanned=scanned, indexed=indexed, skipped=skipped, errors=errors).__dict__
