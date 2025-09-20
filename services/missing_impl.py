
"""
services/missing_impl.py

Hotfix: Provide the missing functions used in handlers/telegram_core.py:
- embed_query
- _build_prompt
- _llm_answer_no_rag
- _llm_answer_with_rag

These are registered into `builtins` so that existing code that calls them without imports will work.
This approach avoids invasive edits to your existing files. If later you implement first-class
versions elsewhere, you can remove this shim.
"""

from __future__ import annotations

import asyncio
import logging
import builtins
from typing import List, Dict, Any

# Try to import your existing OpenAI helper.
# We intentionally support both "bot.openai_helper" and "openai_helper".
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
    """
    Accepts various chunk formats and extracts text.
    Supports dicts like {'content': '...'} or {'text': '...'} or {'page_content': '...'}.
    """
    if isinstance(chunk, dict):
        for k in ("content", "text", "page_content", "body"):
            if k in chunk and chunk[k]:
                return _coerce_text(chunk[k])
    return _coerce_text(chunk)

async def embed_query(text: str) -> List[float]:
    """
    Async wrapper to produce a single embedding vector for the provided text.

    Expected to be used as:
        embedding_vec = await embed_query("some query")
    """
    text = _coerce_text(text).strip()
    if not text:
        return []

    if _openai_helper and hasattr(_openai_helper, "embed"):
        try:
            # Your helper is expected to be async and accept List[str] -> List[List[float]]
            embs = await _openai_helper.embed([text])
            if embs and isinstance(embs, list):
                return embs[0] if embs else []
        except Exception as e:
            logger.exception("embed_query via openai_helper.embed failed: %s", e)

    # Minimal local fallback using tiktoken-like hashing (NOT semantic). Keeps code from crashing.
    # Strongly recommended to keep using your real embeddings via openai_helper.
    import hashlib, math
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # produce a small fixed-size pseudo-vector
    vec = [((h[i] / 255.0) - 0.5) for i in range(64)]
    # l2 normalize
    norm = math.sqrt(sum(v*v for v in vec)) or 1.0
    return [v / norm for v in vec]

def _build_prompt(user_q: str, chunks: List[Any]) -> str:
    """
    Build a plain-text prompt for RAG answers.
    """
    user_q = _coerce_text(user_q).strip()
    safe_chunks = chunks or []
    # Deduplicate and trim chunk texts
    texts = []
    seen = set()
    for ch in safe_chunks:
        t = _extract_chunk_text(ch).strip()
        if not t:
            continue
        key = (t[:80], len(t))
        if key in seen:
            continue
        seen.add(key)
        texts.append(t)

    context_block = "\n\n---\n".join(texts[:10])  # keep the prompt bounded
    instr = (
        "Ты — аккуратный ассистент. Используй контекст ниже, цитируй коротко, "
        "если уместно укажи источник, и ясно отвечай по делу. Если в контексте "
        "нет ответа, скажи об этом и ответь, опираясь на общее знание."
    )
    prompt = f"{instr}\n\n[КОНТЕКСТ]\n{context_block}\n\n[ВОПРОС]\n{user_q}\n\n[ФОРМАТ]\nКратко, структурировано."
    return prompt

async def _llm_answer_no_rag(user_q: str) -> str:
    """
    Produce a model answer without RAG context.
    """
    user_q = _coerce_text(user_q)
    if _openai_helper and hasattr(_openai_helper, "chat"):
        try:
            # Expected to be: async chat(messages: List[dict]) -> str
            return await _openai_helper.chat([
                {"role": "system", "content": "Ты — полезный, точный и лаконичный ассистент."},
                {"role": "user", "content": user_q},
            ])
        except Exception as e:
            logger.exception("_llm_answer_no_rag via openai_helper.chat failed: %s", e)

    # Fallback when chat is unavailable, to avoid crashing the bot.
    return "Извини, сейчас не могу сгенерировать развернутый ответ. Проверь настройки доступа к модели."

async def _llm_answer_with_rag(prompt: str) -> str:
    """
    Produce a model answer using a prepared prompt that already contains context.
    """
    prompt = _coerce_text(prompt)
    if _openai_helper and hasattr(_openai_helper, "chat"):
        try:
            return await _openai_helper.chat([
                {"role": "system", "content": "Ты — полезный, точный и лаконичный ассистент."},
                {"role": "user", "content": prompt},
            ])
        except Exception as e:
            logger.exception("_llm_answer_with_rag via openai_helper.chat failed: %s", e)
    return "Извини, не удалось получить ответ от модели. Проверь ключ и параметры модели."

# Register into builtins so that modules that reference these names without import will still find them.
builtins.embed_query = embed_query
builtins._build_prompt = _build_prompt
builtins._llm_answer_no_rag = _llm_answer_no_rag
builtins._llm_answer_with_rag = _llm_answer_with_rag

logger.info("Hotfix registered: embed_query, _build_prompt, _llm_answer_no_rag, _llm_answer_with_rag")
