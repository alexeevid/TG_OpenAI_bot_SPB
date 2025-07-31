# bot/openai_helper.py
from __future__ import annotations

import io
import base64
import logging
from typing import List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from openai import OpenAI

logger = logging.getLogger(__name__)


def _style_to_system_prompt(style: str) -> str:
    style = (style or "Pro").lower()
    if style == "pro":
        return ("Отвечай как высокопрофессиональный консультант: кратко, точно, без воды; "
                "ссылайся на данные из контекста, если они есть.")
    if style == "expert":
        return ("Отвечай как эксперт в теме: раскрывай глубину, объясняй логику и компромиссы; "
                "но избегай лишней воды.")
    if style == "user":
        return ("Объясняй просто и по-человечески, как обычный опытный пользователь, без жаргона.")
    if style == "ceo":
        return ("Отвечай как владелец бизнеса с уровнем EMBA/DBA: акценты на ценности, рисках, ROI, стратегии.")
    return "Отвечай чётко и по существу."


def _build_messages(user_text: str, style: str, kb_context: Optional[str]) -> List[dict]:
    sys_hint = _style_to_system_prompt(style)
    msgs: List[dict] = [{"role": "system", "content": sys_hint}]
    if kb_context:
        msgs.append({"role": "system", "content": f"Контекст из базы знаний:\n{kb_context}"})
    msgs.append({"role": "user", "content": user_text})
    return msgs


class OpenAIHelper:
    """
    Минимальная совместимая обёртка под новый openai SDK (>=1.30).
    Сигнатура конструктора — только api_key, чтобы не было конфликтов.
    """
    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)

    # --- Chat ----------------------------------------------------------------
    def chat(
        self,
        user_text: str,
        model: Optional[str],
        temperature: float,
        style: str,
        kb_context: Optional[str],
    ) -> str:
        model_name = model or "gpt-4o"
        try:
            msgs = _build_messages(user_text, style, kb_context)
            cc = self.client.chat.completions.create(
                model=model_name,
                messages=msgs,
                temperature=temperature,
            )
            return cc.choices[0].message.content or ""
        except Exception as e:
            logger.exception("chat() failed: %s", e)
            raise

    # --- STT -----------------------------------------------------------------
    def transcribe_audio(self, wav_or_ogg_bytes: bytes) -> str:
        try:
            # whisper-1 — стабильный вариант
            with io.BytesIO(wav_or_ogg_bytes) as fh:
                fh.name = "audio.ogg"  # имя нужно SDK
                tr = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=fh,
                    response_format="text",
                    temperature=0.0,
                )
            return tr  # response_format=text возвращает строку
        except Exception as e:
            logger.exception("transcribe_audio() failed: %s", e)
            raise

    # --- Images --------------------------------------------------------------
    def generate_image(self, prompt: str, model: Optional[str] = None) -> Tuple[bytes, str]:
        """
        Возвращает (image_bytes, used_prompt).
        Пытается сначала указанный model (или dall-e-3), затем gpt-image-1 (если доступ).
        Гарантирует base64→bytes; если API вернул URL — скачиваем URL.
        """
        primary = model or "dall-e-3"
        fallback = "gpt-image-1"

        def _call(m: str) -> Tuple[bytes, str]:
            res = self.client.images.generate(
                model=m,
                prompt=prompt,
                size="1024x1024",
                # просим base64, но иногда API отдаёт url — обработаем ниже
                response_format="b64_json",
            )
            # try base64
            b64 = None
            if res and res.data and hasattr(res.data[0], "b64_json"):
                b64 = res.data[0].b64_json
            if b64:
                return base64.b64decode(b64), prompt

            # fallback to url download
            url = None
            if res and res.data and hasattr(res.data[0], "url"):
                url = res.data[0].url
            if not url:
                raise RuntimeError("Images API did not return base64 image.")

            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return r.content, prompt

        try:
            return _call(primary)
        except Exception as e1:
            logger.warning("Primary image model '%s' failed: %s. Trying '%s' fallback...", primary, e1, fallback)
            return _call(fallback)

    # --- File/Image describe -------------------------------------------------
    def describe_file(self, raw_bytes: bytes, filename: str) -> str:
        """
        Простой «описатель» файла: спрашивает LLM «что внутри и чем полезно» (без реального извлечения текста).
        Если нужен реальный парсинг PDF/Docx — делайте это вне LLM.
        """
        q = f"У меня есть файл '{filename}'. Кратко опиши, что в нём может быть и как его лучше использовать."
        return self.chat(q, model="gpt-4o", temperature=0.2, style="Pro", kb_context=None)

    def describe_image(self, raw_jpeg_bytes: bytes) -> str:
        """
        Визуальный ввод: используем gpt-4o-mini для анализа изображения.
        """
        try:
            b64 = base64.b64encode(raw_jpeg_bytes).decode("ascii")
            cc = self.client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.2,
                messages=[
                    {"role": "system", "content": "Кратко опиши изображение и сделай 2-3 наблюдения."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Что на фото?"},
                            {"type": "input_image", "image_url": {"url": f"data:image/jpeg;base64,{b64}" }},
                        ],
                    },
                ],
            )
            return cc.choices[0].message.content or "Нет ответа."
        except Exception as e:
            logger.exception("describe_image() failed: %s", e)
            raise

    # --- Web search ----------------------------------------------------------
    def web_answer(self, query: str) -> Tuple[str, List[str]]:
        """
        Очень простой веб-поиск: DuckDuckGo (html endpoint), парсим ссылки,
        затем просим LLM ответить, приложив список источников.
        """
        try:
            s = requests.Session()
            s.headers.update({"User-Agent": "Mozilla/5.0"})
            # 1) Идём на /html — он как раз и нужен
            r = s.get("https://html.duckduckgo.com/html", params={"q": query}, timeout=25, allow_redirects=True)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            out = []
            for a in soup.select("a.result__a"):
                href = a.get("href")
                if href and href.startswith("http"):
                    out.append(href)
                if len(out) >= 5:
                    break

            sources = out
            # 2) Составим ответ
            prompt = (
                "Дай краткий ответ на вопрос, основываясь ТОЛЬКО на этих источниках (если они релевантны). "
                "Если недостаточно данных — так и скажи.\n\n"
                f"Вопрос: {query}\n\nИсточники:\n" + "\n".join(f"- {u}" for u in sources)
            )
            ans = self.chat(prompt, model="gpt-4o-mini", temperature=0.2, style="Pro", kb_context=None)
            return ans, sources
        except Exception as e:
            logger.exception("web_answer() failed: %s", e)
            raise
