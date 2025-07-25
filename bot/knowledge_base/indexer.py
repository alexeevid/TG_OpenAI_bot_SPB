
import hashlib
import io
import logging
from typing import Optional

from sqlalchemy import select, delete
from PyPDF2 import PdfReader
import docx2txt

from bot.db.session import SessionLocal
from bot.db.models import Document, DocumentChunk
from bot.knowledge_base.yandex_client import YandexDiskClient

def _split_text(text: str, chunk_size: int = 1200, overlap: int = 200) -> list[str]:
    res = []
    i = 0
    n = len(text)
    while i < n:
        res.append(text[i:i+chunk_size])
        i += chunk_size - overlap
    return res

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _extract_text(file_bytes: bytes, filename: str, pdf_password: Optional[str]) -> str:
    if filename.lower().endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            if reader.is_encrypted:
                if pdf_password:
                    reader.decrypt(pdf_password)
                else:
                    raise RuntimeError("PDF запаролен, пароль не передан.")
            text = []
            for page in reader.pages:
                page_text = page.extract_text() or ""
                text.append(page_text)
            return "\n".join(text)
        except Exception as e:
            raise RuntimeError(f"Ошибка чтения PDF: {e}")
    elif filename.lower().endswith(".docx"):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=True) as tmp:
            tmp.write(file_bytes)
            tmp.flush()
            return docx2txt.process(tmp.name) or ""
    else:
        try:
            return file_bytes.decode("utf-8", errors="ignore")
        except Exception:
            return file_bytes.decode("latin-1", errors="ignore")

async def sync_yandex_to_db(
    yandex_token: str,
    root_path: str,
    embedder,
    embedding_model: str,
    pdf_passwords: dict[str, str] | None = None,
    chunk_size: int = 1200,
    overlap: int = 200,
):
    pdf_passwords = pdf_passwords or {}
    yd = YandexDiskClient(token=yandex_token)
    files = list(yd.iter_files(root_path))
    logging.info("Найдено файлов на Я.Диске: %d", len(files))

    with SessionLocal() as s:
        for href, size in files:
            filename = href.split("/")[-1]
            content = yd.download(href)
            sha = _sha256(content)

            doc = s.execute(select(Document).where(Document.path == href)).scalar_one_or_none()
            if doc and doc.sha256 == sha and doc.size == size:
                continue

            if doc:
                s.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
            else:
                doc = Document(path=href, size=size, sha256=sha)
                s.add(doc)
                s.flush()

            try:
                password = pdf_passwords.get(filename)
                text = _extract_text(content, filename, password)
            except Exception as e:
                logging.error("Не смогли извлечь %s: %s", href, e, exc_info=True)
                s.rollback()
                continue

            chunks = _split_text(text, chunk_size=chunk_size, overlap=overlap)
            if not chunks:
                logging.warning("Пустой документ: %s", href)
                doc.size = size
                doc.sha256 = sha
                s.commit()
                continue

            embs = await embedder(chunks, embedding_model)

            for idx, (txt, emb) in enumerate(zip(chunks, embs)):
                ch = DocumentChunk(
                    document_id=doc.id,
                    chunk_index=idx,
                    text=txt,
                    embedding=emb,
                )
                s.add(ch)

            doc.size = size
            doc.sha256 = sha
            s.commit()

    logging.info("Синхронизация завершена")
