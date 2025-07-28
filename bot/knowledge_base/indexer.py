from typing import List
from sqlalchemy.orm import Session
from bot.db.models import Document
from bot.knowledge_base.yandex_rest import YandexDiskREST

def sync_disk_to_db(db: Session, token: str, root_path: str) -> int:
    client = YandexDiskREST(token)
    items = client.list_all_files(root_path)
    added = 0
    for it in items:
        path = it['path']
        title = it.get('name') or path.split('/')[-1]
        mime = it.get('mime_type')
        size = it.get('size')
        exists = db.query(Document).filter_by(path=path).first()
        if not exists:
            db.add(Document(path=path, title=title, mime=mime, size=size))
            added += 1
    db.commit()
    return added
