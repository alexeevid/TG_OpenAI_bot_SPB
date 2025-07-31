# bot/openai_helper.py
from __future__ import annotations

import base64
import io
import logging
from typing import List, Optional, Tuple

from openai import OpenAI

logger = logging.getLogger(__name__)


class OpenAIHelper:
    """
    Минимальный совместимый хелпер с поддержкой:
    - chat(..., kb_context, kb_strict)
    - embed_texts(texts)
    - transcribe_audio(bytes)
    - generate_image(prompt, model=None) -> (bytes, used_prompt)
    - web_answer(query) [простая заглушка без реального браузера]
    - list_models_for_menu() [фильтр по разумным моделям]
    """

    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)

    # ---------- Embeddings ----------

    def embed_texts(self, texts: List[str], model: str = "text-embedding-3-small") -> List[List[float]]:
        resp = self.client.embeddings.create(model=model, input=texts)
        return [d.embedding for d in resp.data]

    # ---------- Chat with RAG ----------

    def chat(
        self,
        user_text: str,
        model: Optional[str],
        temperature: float,
        style: str,
        kb_context: Optional[str] = None,
        kb_strict: bool = True,
    ) -> str:
        model = model or "gpt-4o"
        sys_style = self._style_hint(style)

        system = sys_style
        if kb_context:
            if kb_strict:
                system += (
                    "\n\nYou are in STRICT knowledge-base mode.\n"
                    "Answer ONLY using the information from KB_CONTEXT below. "
                    "If the KB_CONTEXT is insufficient, say that the information is not found in the knowledge base.\n"
                    "KB_CONTEXT:\n" + kb_context
                )
            else:
                system += (
                    "\n\nYou are in HYBRID knowledge-base mode.\n"
                    "Prefer the information from KB_CONTEXT below. "
                    "If it is insufficient, you MAY use your own knowledge to fill gaps.\n"
                    "KB_CONTEXT:\n" + kb_context
                )

        resp = self.client.chat.completions.create(
            model=model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
        )
        return resp.choices[0].message.content.strip()

    @staticmethod
    def _style_hint(style: str) -> str:
        style = (style or "Pro").lower()
        if style == "pro":
            return ("You are a highly experienced professional. "
                    "Be concise, precise, and context-aware. Provide only what is needed.")
        if style == "expert":
            return ("You are a deep subject-matter expert. "
                    "Give rigorous, well-structured answers with explicit assumptions.")
        if style == "user":
            return ("You speak like a non-expert end user. "
                    "Use simple words, short sentences, practical tips.")
        if style == "ceo":
            return ("You speak like a CEO with EMBA/DBA perspective. "
                    "Be strategic, focus on business impact, risks, and decisions.")
        return "You are helpful and concise."

    # ---------- Whisper ----------

    def transcribe_audio(self, audio_bytes: bytes, model: str = "whisper-1") -> str:
        file = io.BytesIO(audio_bytes)
        file.name = "audio.ogg"
        tr = self.client.audio.transcriptions.create(model=model, file=file, response_format="text", language="ru")
        return tr

    # ---------- Images ----------

    def generate_image(self, prompt: str, model: Optional[str] = None) -> Tuple[bytes, str]:
        """
        Возвращает (image_bytes, used_prompt).
        Пытаемся 'gpt-image-1' -> фолбэк 'dall-e-3'.
        """
        primary = model or "gpt-image-1"
        def _call(m: str) -> Tuple[bytes, str]:
            res = self.client.images.generate(model=m, prompt=prompt, size="1024x1024")
            data = res.data[0]
            b64 = getattr(data, "b64_json", None)
            if not b64:
                raise RuntimeError("Images API did not return base64 image.")
            return base64.b64decode(b64), prompt

        try:
            return _call(primary)
        except Exception as e:
            logger.warning("Primary image model '%s' failed: %s. Trying 'dall-e-3' fallback...", primary, e)
            return _call("dall-e-3")

    # ---------- Web (простая заглушка через модель) ----------

    def web_answer(self, query: str) -> Tuple[str, List[str]]:
        """
        Без реального браузера: просим модель дать ответ + ссылки,
        но она может вернуть «без явных ссылок».
        """
        sys = "You are a web research assistant. Always include 3-5 URLs if possible."
        resp = self.client.chat.completions.create(
            model="gpt-4o",
            temperature=0.2,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": f"Query: {query}\nPlease answer in Russian and list sources."},
            ],
        )
        text = resp.choices[0].message.content.strip()
        # простая эвристика извлечения ссылок
        import re
        urls = re.findall(r"https?://\S+", text)
        return text, urls[:5]

    # ---------- Models menu ----------

    def list_models_for_menu(self) -> List[str]:
        """
        Возвращаем разумный короткий список (часть моделей реально недоступна по аккаунтам).
        Если у вас есть whitelisting в settings — лучше фильтровать там.
        """
        return [
            "gpt-4o",
            "gpt-4o-mini",
            "o4-mini",
            "gpt-4.1-mini",
            "gpt-3.5-turbo",
        ]
