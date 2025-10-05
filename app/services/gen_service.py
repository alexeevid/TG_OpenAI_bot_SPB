from ..clients.openai_client import OpenAIClient
from ..core.types import ModelAnswer
from .rag_service import RagService

class GenService:
    def __init__(self, openai: OpenAIClient, rag: RagService, settings):
        self._openai = openai
        self._rag = rag
        self._s = settings

    async def chat(self, *, user_msg: str, dialog_id: int) -> ModelAnswer:
        # RAG-контекст (не критичен — если пусто, не мешает)
        try:
            ctx_chunks = self._rag.retrieve(user_msg, dialog_id, self._s.max_kb_chunks)
        except Exception:
            ctx_chunks = []

        sys = "You are a helpful assistant. Use provided context snippets if relevant."
        context_text = "\n\n".join(f"[{i+1}] {c.text}" for i, c in enumerate(ctx_chunks or []))
        content = user_msg
        if context_text:
            content = f"Context:\n{context_text}\n\nQuestion: {user_msg}"
        msg = [{"role": "system", "content": sys}, {"role": "user", "content": content}]

        # Основной вызов OpenAI
        text = ""
        try:
            text = (self._openai.chat(msg, model=self._s.text_model, temperature=self._s.temperature) or "").strip()
        except Exception:
            # Не пробрасываем — верхний уровень оповестит пользователя
            text = ""

        # Надёжный фолбэк, чтобы не возвращать пусто
        if not text:
            text = "Не удалось получить ответ от модели."

        return ModelAnswer(text=text, citations=ctx_chunks or None)
