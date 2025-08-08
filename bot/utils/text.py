from __future__ import annotations
from typing import List

def chunk_text(text: str, size: int, overlap: int) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    chunks = []
    i = 0
    n = len(text)
    while i < n:
        j = min(n, i + size)
        chunks.append(text[i:j])
        i = j - overlap if j - overlap > i else j
    return chunks
