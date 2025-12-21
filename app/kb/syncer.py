import math
from sqlalchemy import text as sqltext

class KBSyncer:
    def __init__(self, yandex_client, embedder, kb_repo, settings):
        self.yd = yandex_client
        self.embedder = embedder
        self.kb = kb_repo
        self.settings = settings

    def sync(self):
        # If Yandex Disk not configured, nothing to do
        if not getattr(self.yd, "token", None):
            return {"status": "error", "message": "Yandex Disk client not configured"}
        # List root knowledge base directory
        try:
            data = self.yd.list("")
        except Exception as e:
            return {"status": "error", "message": str(e)}
        items = []
        if isinstance(data, dict):
            embedded = data.get("_embedded")
            if embedded:
                items = embedded.get("items", [])
        # Iterate files in KB root
        indexed_chunks = 0
        for item in items:
            if item.get("type") != "file":
                continue
            file_name = item.get("name", "")
            if not file_name:
                continue
            # Download file content
            try:
                content_bytes = self.yd.download(file_name)
            except Exception as e:
                return {"status": "error", "message": f"Failed to download {file_name}: {e}"}
            try:
                text_content = content_bytes.decode("utf-8")
            except Exception:
                try:
                    text_content = content_bytes.decode("cp1251")
                except Exception:
                    text_content = content_bytes.decode("utf-8", errors="ignore")
            # Create or get document record
            doc_id = self.kb.upsert_document(path=file_name, title=file_name)
            # Remove old chunks for this document to avoid duplicates
            with self.kb.sf() as session:
                session.execute(sqltext("DELETE FROM kb_chunks WHERE document_id = :doc"), {"doc": doc_id})
                session.commit()
            # Split content into chunks with overlap
            chunk_size = getattr(self.settings, "chunk_size", 900) or 900
            chunk_overlap = getattr(self.settings, "chunk_overlap", 150) or 150
            text_len = len(text_content)
            chunks = []
            start = 0
            while start < text_len:
                end = min(text_len, start + chunk_size)
                chunk_text = text_content[start:end]
                # Try to cut at word boundary for nicer chunk (optional)
                if end < text_len:
                    cut = chunk_text.rfind(" ")
                    if cut != -1 and cut > len(chunk_text) - 100:
                        chunk_text = chunk_text[:cut]
                        end = start + len(chunk_text)
                chunk_text = chunk_text.strip()
                if chunk_text:
                    chunks.append(chunk_text)
                if end >= text_len:
                    break
                start = end - chunk_overlap
            # Embed all chunks and store in database
            try:
                embeddings = self.embedder.embed(chunks)
            except Exception as e:
                return {"status": "error", "message": f"Embedding failed: {e}"}
            for chunk_text, emb in zip(chunks, embeddings):
                self.kb.insert_chunk(document_id=doc_id, text=chunk_text, embedding=emb)
            indexed_chunks += len(chunks)
        return {"status": "ok", "indexed": indexed_chunks}
