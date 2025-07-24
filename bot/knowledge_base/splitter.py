def split_text(text: str, chunk_size_tokens: int = 1000, overlap_tokens: int = 100) -> list[str]:
    size = chunk_size_tokens * 4
    step = size - overlap_tokens * 4
    return [text[i:i+size] for i in range(0, len(text), step)]
