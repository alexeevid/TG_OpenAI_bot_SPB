
def trim_to_token_limit(text: str, limit: int) -> str:
    return text[:limit]

def build_context_messages(docs: list[str]) -> str:
    return "\n\n".join(docs)
