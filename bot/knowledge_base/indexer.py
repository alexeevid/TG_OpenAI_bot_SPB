
import logging
from typing import List, Dict
from sqlalchemy import select
from bot.db.session import SessionLocal
from bot.db.models import Document
from .yandex_rest import YandexDiskREST

def sync_disk_to_db(token: str, root_path: str) -> int:
    yd = YandexDiskREST(token)
    files: List[Dict] = yd.list_all_files(root_path)

    with SessionLocal() as s:
        existing = {d.path: d for d in s.scalars(select(Document)).all()}
        added = 0
        for f in files:
            if f["path"] not in existing:
                doc = Document(path=f["path"], size=f["size"])
                s.add(doc)
                added += 1
        s.commit()
    logging.info("Synced %s files from Yandex Disk", len(files))
    return added
