def split_text(text: str, chunk_size_tokens: int = 1000, overlap_tokens: int = 100) -> list[str]:
    size = chunk_size_tokens * 4
    step = size - overlap_tokens * 4
    chunks = []
    for i in range(0, len(text), step):
        chunks.append(text[i:i+size])
    return chunks
