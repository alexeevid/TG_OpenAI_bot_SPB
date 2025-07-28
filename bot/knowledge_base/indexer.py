from typing import List
from sqlalchemy.orm import Session
from bot.db.models import Document
from bot.knowledge_base.yandex_rest import YandexDiskREST
import hashlib
from datetime import datetime

def _fallback_sha256(path: str, title: str, mime: str | None, size: int | None) -> str:
    """Детерминированный sha256, если API не вернул checksum."""
    h = hashlib.sha256()
    h.update((path or "").encode("utf-8"))
    h.update((title or "").encode("utf-8"))
    h.update((mime or "").encode("utf-8"))
    h.update(str(size or 0).encode("utf-8"))
    return h.hexdigest()

def sync_disk_to_db(db: Session, token: str, root_path: str) -> int:
    client = YandexDiskREST(token)
    items = client.list_all_files(root_path)
    added = 0

    for it in items:
        path = it["path"]
        title = it.get("name") or path.split("/")[-1]
        mime = it.get("mime_type")
        size = it.get("size")

        # Если API отдаёт sha256 — используем, иначе считаем fallback
        sha = it.get("sha256") or _fallback_sha256(path, title, mime, size)

        exists = db.query(Document).filter_by(path=path).first()
        if not exists:
            db.add(
                Document(
                    path=path,
                    title=title,
                    sha256=sha,
                    mime=mime,
                    size=size,
                    created_at=datetime.utcnow(),
                )
            )
            added += 1

    db.commit()
    return added
