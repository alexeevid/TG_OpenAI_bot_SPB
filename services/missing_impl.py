
"""
services/missing_impl.py
(see v2) — same as in v1 with minor tweaks
"""
from __future__ import annotations
import asyncio, logging, builtins, hashlib, math
from typing import List, Any

# Try both names, depending on project layout
_openai_helper = None
for _mod in ("bot.openai_helper", "openai_helper"):
    try:
        _openai_helper = __import__(_mod, fromlist=["*"])
        break
    except Exception:
        continue

logger = logging.getLogger(__name__)
if not logger.handlers:
    h = logging.StreamHandler()
    fmt = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
    h.setFormatter(fmt)
    logger.addHandler(h)
logger.setLevel(logging.INFO)

def _coerce_text(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, (bytes, bytearray)):
        try:
            return x.decode("utf-8", "ignore")
        except Exception:
            return str(x)
    return str(x)

def _extract_chunk_text(chunk: Any) -> str:
    if isinstance(chunk, dict):
        for k in ("content", "text", "page_content", "body"):
            if k in chunk and chunk[k]:
                return _coerce_text(chunk[k])
    return _coerce_text(chunk)

async def embed_query(text: str) -> List[float]:
    text = _coerce_text(text).strip()
    if not text:
        return []
    if _openai_helper and hasattr(_openai_helper, "embed"):
        try:
            embs = await _openai_helper.embed([text])
            if embs and isinstance(embs, list):
                return embs[0] if embs else []
        except Exception as e:
            logger.exception("embed_query via openai_helper.embed failed: %s", e)
    # Fallback pseudo-embedding (keeps bot running, not semantic)
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vec = [((h[i] / 255.0) - 0.5) for i in range(64)]
    norm = math.sqrt(sum(v*v for v in vec)) or 1.0
    return [v / norm for v in vec]

def _build_prompt(user_q: str, chunks: list[Any]) -> str:
    user_q = _coerce_text(user_q).strip()
    texts, seen = [], set()
    for ch in (chunks or []):
        t = _extract_chunk_text(ch).strip()
        if not t:
            continue
        key = (t[:80], len(t))
        if key in seen: 
            continue
        seen.add(key)
        texts.append(t)
    context_block = "\n\n---\n".join(texts[:10])
    instr = ("Ты — аккуратный ассистент. Используй контекст ниже, цитируй коротко, "
             "если уместно укажи источник. Если в контексте нет ответа — скажи об этом.")
    return f"{instr}\n\n[КОНТЕКСТ]\n{context_block}\n\n[ВОПРОС]\n{user_q}\n\n[ФОРМАТ]\nКратко, структурировано."

async def _llm_answer_no_rag(user_q: str) -> str:
    user_q = _coerce_text(user_q)
    if _openai_helper and hasattr(_openai_helper, "chat"):
        try:
            return await _openai_helper.chat([
                {"role": "system", "content": "Ты — полезный, точный и лаконичный ассистент."},
                {"role": "user", "content": user_q},
            ])
        except Exception as e:
            logger.exception("_llm_answer_no_rag failed: %s", e)
    return "Извини, не могу сгенерировать ответ (проверь ключ/модель)."

async def _llm_answer_with_rag(prompt: str) -> str:
    prompt = _coerce_text(prompt)
    if _openai_helper and hasattr(_openai_helper, "chat"):
        try:
            return await _openai_helper.chat([
                {"role": "system", "content": "Ты — полезный, точный и лаконичный ассистент."},
                {"role": "user", "content": prompt},
            ])
        except Exception as e:
            logger.exception("_llm_answer_with_rag failed: %s", e)
    return "Извини, не удалось получить ответ от модели."

# export into builtins so existing code finds them
builtins.embed_query = embed_query
builtins._build_prompt = _build_prompt
builtins._llm_answer_no_rag = _llm_answer_no_rag
builtins._llm_answer_with_rag = _llm_answer_with_rag

logger.info("Hotfix v2 registered: embed_query/_build_prompt/_llm_answer_*")
