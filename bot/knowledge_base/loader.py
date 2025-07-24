import io
import hashlib
from typing import Optional
from bot.db.session import SessionLocal
from bot.db.models import Document, DocumentChunk
from bot.knowledge_base.yandex_client import YandexDiskClient
from bot.knowledge_base.splitter import split_text
from bot.knowledge_base.passwords import get_pdf_password
import PyPDF2
import docx2txt

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def extract_text_from_pdf(content: bytes, password: Optional[str] = None) -> str:
    reader = PyPDF2.PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        if password:
            reader.decrypt(password)
        else:
            raise RuntimeError("PDF encrypted")
    texts = []
    for page in reader.pages:
        texts.append(page.extract_text() or "")
    return "\n".join(texts)

def extract_text_from_docx(content: bytes) -> str:
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp.write(content)
        path = tmp.name
    try:
        return docx2txt.process(path) or ""
    finally:
        try: os.remove(path)
        except Exception: pass

def detect_password_need(path: str, content: bytes) -> bool:
    if path.lower().endswith(".pdf"):
        try:
            _ = extract_text_from_pdf(content, password=None)
            return False
        except Exception:
            return True
    return False

def guess_text(path: str, content: bytes, password: Optional[str] = None) -> str:
    lower = path.lower()
    if lower.endswith(".pdf"):
        return extract_text_from_pdf(content, password=password)
    elif lower.endswith(".docx"):
        return extract_text_from_docx(content)
    elif lower.endswith(".txt") or lower.endswith(".md"):
        return content.decode("utf-8", errors="ignore")
    else:
        return ""

async def sync_yandex_disk_to_db(token: str, base_url: str, root_path: str, embedding_client, embedding_model: str):
    yd = YandexDiskClient(token=token, base_url=base_url)
    files = list(yd.iter_files(root_path))
    with SessionLocal() as s:
        existing = {d.path: d for d in s.query(Document).all()}
        for path, size in files:
            content = yd.download(path)
            sha = sha256_bytes(content)
            doc = existing.get(path)

            password_required = detect_password_need(path, content)
            text = ""
            if not password_required:
                pwd = get_pdf_password(path)
                try:
                    text = guess_text(path, content, password=pwd)
                except Exception:
                    password_required = True

            if doc:
                s.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).delete()
                doc.size = size
                doc.sha256 = sha
                doc.password_required = password_required
            else:
                doc = Document(path=path, size=size, sha256=sha, password_required=password_required)
                s.add(doc)
                s.flush()

            if not password_required and text.strip():
                chunks = split_text(text)
                for idx, ch in enumerate(chunks):
                    s.add(DocumentChunk(document_id=doc.id, chunk_index=idx, text=ch, embedding=[0.0]*3072))
            s.commit()

        disk_paths = set(p for p, _ in files)
        for p, d in existing.items():
            if p not in disk_paths:
                s.query(DocumentChunk).filter(DocumentChunk.document_id == d.id).delete()
                s.delete(d)
        s.commit()
