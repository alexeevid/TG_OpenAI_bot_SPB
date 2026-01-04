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
    Синхронизация базы знаний (Яндекс.Диск -> kb_documents + kb_chunks).

    Best practice:
    - Единственный реестр документов: kb_documents
    - Delete-propagation: файлы, исчезнувшие с Диска, становятся is_active=false
    - Индексация делается через KbIndexer (эмбеддинги -> pgvector)
    - Пароли PDF НЕ храним глобально: только per-dialog (dialog_kb_secrets) — это решается отдельно
    """

    def __init__(self, settings: Settings, repo: KBRepo, indexer: KbIndexer, yandex_client: Any):
        self._cfg = settings
        self._repo = repo
        self._indexer = indexer
        self._y = yandex_client

    def _parse_to_text(self, filename: str, data: bytes) -> str:
        ext = detect_ext(filename)

        if ext in ("txt", "md", "log"):
            return parse_txt_bytes(data)

        if ext in ("pdf",):
            return parse_pdf_bytes(data)

        if ext in ("docx",):
            return parse_docx_bytes(data)

        if ext in ("xlsx", "xls"):
            return parse_xlsx_bytes(data)

        if ext in ("csv",):
            return parse_csv_bytes(data)

        if is_image_ext(ext):
            # best effort: иногда из изображений есть смысл извлечь хоть что-то
            return parse_image_bytes_best_effort(data)

        # неизвестный формат -> пусто (пропускаем)
        return ""

    def run(self) -> SyncResult:
        """
        Полный проход:
        1) Получаем список файлов БЗ с Я.Диска (metadata)
        2) Помечаем все документы is_active=false
        3) Для каждого файла:
           - upsert kb_documents (is_active=true + метаданные)
           - если md5/modified изменился — перегенерируем chunks+embeddings
        """
        res = SyncResult()

        files = self._y.list_kb_files_metadata()  # must return list[dict]
        res.scanned = len(files)

        # Delete propagation: по умолчанию всё "неактивно", оживляем по факту скана
        try:
            self._repo.mark_all_documents_inactive()
        except Exception as e:
            log.warning("mark_all_documents_inactive failed (continue): %s", e)

        for f in files:
            try:
                path = f.get("path") or f.get("full_path") or ""
                if not path:
                    res.skipped += 1
                    continue

                title = f.get("name") or f.get("title") or path.split("/")[-1]
                resource_id = f.get("resource_id")
                mime_type = f.get("mime_type")
                md5 = f.get("md5")
                size = f.get("size")
                modified_at = f.get("modified_at")

                # modified_at может быть str -> datetime; repo обычно умеет сам, но подстрахуемся
                if isinstance(modified_at, str):
                    try:
                        modified_at = datetime.fromisoformat(modified_at.replace("Z", "+00:00"))
                    except Exception:
                        modified_at = None

                doc = self._repo.upsert_document(
                    path=path,
                    title=title,
                    resource_id=resource_id,
                    mime_type=mime_type,
                    md5=md5,
                    size=size,
                    modified_at=modified_at,
                    is_active=True,
                )

                # решаем, нужно ли переиндексировать
                needs_reindex = self._repo.document_needs_reindex(doc_id=doc.id, md5=md5, modified_at=modified_at)
                if not needs_reindex:
                    res.skipped += 1
                    continue

                # скачиваем контент
                data: bytes = self._y.download_bytes(path)
                text = self._parse_to_text(title, data).strip()

                # если PDF запаролен — парсер обычно бросит исключение/вернёт пусто
                # repo фиксирует флаг pdf_password_required, чтобы UI мог запросить пароль в диалоге
                if not text:
                    self._repo.set_pdf_password_required(doc.id, True)
                    res.skipped += 1
                    continue

                self._repo.set_pdf_password_required(doc.id, False)

                # индексируем
                self._indexer.reindex_document(doc_id=doc.id, document_text=text)
                res.indexed += 1

            except Exception as e:
                log.exception("KB sync failed for file=%s: %s", f, e)
                res.errors += 1

        return res


# ------------------------------------------------------------
# Backward compatibility / stable public API name
# ------------------------------------------------------------
# app.main импортирует KBSyncer, а часть модулей может использовать KbSyncer.
# Держим оба имени, чтобы правки в одном месте не аффектили остальное.
KBSyncer = KbSyncer
