import hashlib
from .yandex_rest import YandexDiskREST
from ..db.session import SessionLocal, engine, Base
from ..db.models import Document
import logging

def sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()

def sync_yandex_to_db(token: str, root_path: str):
    if engine is None or SessionLocal is None:
        raise RuntimeError("DATABASE_URL is not configured; can't sync to DB.")
    Base.metadata.create_all(engine)
    yd = YandexDiskREST(token)
    session = SessionLocal()
    try:
        for item in yd.list_recursive(root_path):
            if item['type'] != 'file':
                continue
            path = item['path']
            size = item.get('size', 0)
            doc = session.query(Document).filter_by(path=path).first()
            if not doc:
                doc = Document(path=path, size=size)
                session.add(doc)
            else:
                doc.size = size
        session.commit()
        logging.info("KB sync done")
    finally:
        session.close()
