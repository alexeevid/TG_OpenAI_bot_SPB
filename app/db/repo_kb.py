
from sqlalchemy.orm import Session
from sqlalchemy import text as sqltext
from typing import List, Tuple
from json import dumps, loads

class KBRepo:
    def __init__(self, sf, dim: int):
        self.sf = sf
        self.dim = dim

    def upsert_document(self, path: str, title: str | None):
        from .models import KBDocument
        with self.sf() as s:  # type: Session
            doc = s.query(KBDocument).filter_by(path=path).first()
            if not doc:
                doc = KBDocument(path=path, title=title or path)
                s.add(doc); s.commit(); s.refresh(doc)
            return doc.id

    def insert_chunk(self, document_id: int, text: str, embedding: list[float]):
        from .models import KBChunk
        with self.sf() as s:
            ch = KBChunk(document_id=document_id, text=text, embedding=dumps(embedding))
            s.add(ch); s.commit(); s.refresh(ch); return ch.id

    def search_by_embedding(self, query_emb: list[float], top_k: int) -> List[Tuple[int, str, float]]:
        # косинусная дистанция вручную через SQL — для простоты храним эмбеддинг как json
        with self.sf() as s:
            rows = s.execute(sqltext("SELECT id, text, embedding FROM kb_chunks")).all()
            # Рассчитаем косинусную близость в Python (не самый быстрый, но рабочий вариант)
            def cos_sim(a,b):
                import math
                num = sum(x*y for x,y in zip(a,b))
                da = math.sqrt(sum(x*x for x in a)); db = math.sqrt(sum(y*y for y in b))
                return num/(da*db+1e-9)
            scored = []
            for r in rows:
                emb = loads(r[2])
                score = cos_sim(query_emb, emb)  # чем больше, тем ближе
                scored.append((r[0], r[1], 1.0 - score))  # приведём к "дистанции"
            scored.sort(key=lambda x: x[2])
            return scored[:top_k]
