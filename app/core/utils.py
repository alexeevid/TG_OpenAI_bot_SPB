
import tiktoken
def split_by_tokens(text: str, max_tokens: int, model: str="gpt-4o-mini") -> list[str]:
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
        return [enc.decode(tokens[i:i+max_tokens]) for i in range(0,len(tokens),max_tokens)]
    except Exception:
        return [text[i:i+max_tokens*4] for i in range(0,len(text), max_tokens*4)]
