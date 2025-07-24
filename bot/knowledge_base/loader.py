import io, hashlib, logging, tempfile, os
from typing import Optional
import PyPDF2, docx2txt
from sqlalchemy import select
from bot.db.session import SessionLocal
from bot.db.models import Document, DocumentChunk
from bot.knowledge_base.yandex_client import YandexDiskClient
from bot.knowledge_base.splitter import split_text
from bot.knowledge_base.passwords import get_pdf_password

def sha256_bytes(b: bytes) -> str: return hashlib.sha256(b).hexdigest()

def extract_pdf(content: bytes, password: Optional[str] = None) -> str:
    reader = PyPDF2.PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        if password: reader.decrypt(password)
        else: raise RuntimeError("PDF encrypted")
    return "\n".join((p.extract_text() or "") for p in reader.pages)

def extract_docx(content: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp.write(content); path = tmp.name
    try: return docx2txt.process(path) or ""
    finally: 
        try: os.remove(path)
        except Exception: pass

def detect_password(path: str, content: bytes) -> bool:
    if path.lower().endswith(".pdf"):
        try: extract_pdf(content, None); return False
        except Exception: return True
    return False

def extract_text(path: str, content: bytes, password: Optional[str] = None) -> str:
    p = path.lower()
    if p.endswith(".pdf"): return extract_pdf(content, password)
    if p.endswith(".docx"): return extract_docx(content)
    if p.endswith(".txt") or p.endswith(".md"): return content.decode("utf-8", "ignore")
    return ""

async def sync_yandex_disk_to_db(token, base_url, root_path, embedding_client, embedding_model, chunk_size_tokens=1000, overlap_tokens=100):
    yd = YandexDiskClient(token, base_url)
    files = list(yd.iter_files(root_path))
    with SessionLocal() as s:
        ex = {d.path: d for d in s.query(Document).all()}
        for path, size in files:
            content = yd.download(path)
            sha = sha256_bytes(content)
            doc = ex.get(path)
            pw_req = detect_password(path, content)
            text = ""
            if not pw_req:
                pwd = get_pdf_password(path)
                try: text = extract_text(path, content, pwd)
                except Exception: pw_req = True
            if doc:
                s.query(DocumentChunk).filter(DocumentChunk.document_id==doc.id).delete()
                doc.size=size; doc.sha256=sha; doc.password_required=pw_req
            else:
                doc = Document(path=path, size=size, sha256=sha, password_required=pw_req)
                s.add(doc); s.flush()
            if not pw_req and text.strip():
                chunks = split_text(text, chunk_size_tokens, overlap_tokens)
                if embedding_client:
                    resp = await embedding_client.embeddings.create(model=embedding_model, input=chunks)
                    for i, (t,d) in enumerate(zip(chunks, resp.data)):
                        s.add(DocumentChunk(document_id=doc.id, chunk_index=i, text=t, embedding=d.embedding))
                else:
                    for i, t in enumerate(chunks):
                        s.add(DocumentChunk(document_id=doc.id, chunk_index=i, text=t, embedding=[0.0]*3072))
            s.commit()
        disk_paths = set(p for p,_ in files)
        for p,d in ex.items():
            if p not in disk_paths:
                s.query(DocumentChunk).filter(DocumentChunk.document_id==d.id).delete()
                s.delete(d)
        s.commit()
