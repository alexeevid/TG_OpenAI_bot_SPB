from __future__ import annotations

import io
import re
import base64
import hashlib
import logging
import mimetypes
from typing import List, Tuple, Optional

import requests

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore

logger = logging.getLogger(__name__)

# Режимы ответа: формируем системные подсказки
STYLE_HINTS = {
    "Pro": (
        "Отвечай как опытный профессионал: кратко, по делу, строго в контексте, "
        "используй нумерованные пункты и таблицы при необходимости, избегай воды."
    ),
    "Expert": (
        "Отвечай как отраслевой эксперт с глубокими знаниями, приводя точные термины, "
        "обоснования и ссылки на стандарты и лучшие практики."
    ),
    "User": (
        "Отвечай просто и человеческим языком, как обычный непрофессиональный пользователь. "
        "Делай ответ понятным и дружелюбным."
    ),
    "CEO": (
        "Отвечай как собственник бизнеса уровня EMBA/DBA: стратегически, кратко, с оценкой рисков, "
        "эффектов и приоритетов; предлагай управленческие решения."
    ),
}


def _safe_str(x) -> str:
    try:
        return str(x)
    except Exception:
        return repr(x)


class OpenAIHelper:
    """
    Обёртка над OpenAI SDK (1.x), совместимая с логикой бота.

    Поддерживает:
      - list_models_for_menu()
      - chat(text, model, temperature, style, kb_ctx)
      - generate_image(prompt, model=None) -> (bytes, used_prompt)
      - web_answer(query) -> (answer, sources)
      - transcribe_audio(audio_bytes) -> transcript
      - describe_file(file_bytes, filename) -> str
      - describe_image(image_bytes) -> str
    """

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        image_model: Optional[str] = None,
        temperature: float = 0.2,
        enable_image_generation: bool = True,
    ) -> None:
        if OpenAI is None:
            raise RuntimeError("OpenAI SDK не установлен или недоступен.")
        self.client = OpenAI(api_key=api_key)

        self.default_model = model or "gpt-4o"
        self.image_model = image_model or "dall-e-3"
        self.default_temperature = float(temperature or 0.2)
        self.enable_image_generation = bool(enable_image_generation)

    # ---------------- Models ----------------
    def list_models_for_menu(self) -> List[str]:
        """
        Возвращает список моделей для меню. Пытается показать как можно больше релевантных.
        """
        try:
            models = self.client.models.list()
            ids = [m.id for m in getattr(models, "data", [])]
        except Exception as e:
            logger.warning("list_models failed: %s", e)
            # Запасной набор — на случай ошибки API
            ids = [
                "gpt-4o",
                "gpt-4o-mini",
                "gpt-4.1",
                "o3",
                "o1",
                "gpt-4",
            ]

        def ok(mid: str) -> bool:
            m = mid.lower()
            if "realtime" in m or "batch" in m or "spectral" in m:
                return False
            return any(x in m for x in ["gpt", "o1", "o3"])

        filtered = [m for m in ids if ok(m)]

        # Уберём дубликаты с сохранением порядка
        seen = set()
        out: List[str] = []
        for m in filtered:
            if m not in seen:
                out.append(m)
                seen.add(m)

        # Дефолтную модель показываем первой
        if self.default_model in out:
            out.remove(self.default_model)
        out.insert(0, self.default_model)
        return out[:25]

    # ---------------- Chat ----------------
    def _compose_system(self, style: str, kb_ctx: Optional[str]) -> str:
        hint = STYLE_HINTS.get(style or "Pro", STYLE_HINTS["Pro"])
        ctx = f"\n\n[KNOWLEDGE]\n{kb_ctx}\n[/KNOWLEDGE]" if kb_ctx else ""
        return f"{hint}{ctx}"

    def chat(
        self,
        user_text: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        style: str = "Pro",
        kb_ctx: Optional[str] = None,
    ) -> str:
        """
        Диалог через Responses API (используем поле input).
        """
        mdl = model or self.default_model
        temp = self._clamp_temperature(temperature)
        sys = self._compose_system(style, kb_ctx)
        try:
            rsp = self.client.responses.create(
                model=mdl,
                input=f"[SYSTEM]\n{sys}\n\n[USER]\n{user_text}",
                temperature=temp,
            )
            return self._first_text(rsp) or "(пустой ответ)"
        except Exception as e:
            logger.exception("chat failed: %s", e)
            raise

    def _clamp_temperature(self, t: Optional[float]) -> float:
        if t is None:
            return self.default_temperature
        try:
            v = float(t)
        except Exception:
            v = self.default_temperature
        return max(0.0, min(2.0, v))

    def _first_text(self, rsp) -> str:
        """
        Извлекает текст из Responses API ответа.
        """
        # В некоторых версиях SDK доступно удобное поле output_text
        try:
            for out in (rsp.output_text,):
                if out:
                    return out
        except Exception:
            pass
        # Универсальная попытка
        try:
            if rsp and getattr(rsp, "output", None):
                chunks = rsp.output  # type: ignore
                texts = []
                for ch in chunks:
                    if getattr(ch, "type", "") == "output_text":
                        texts.append(getattr(ch, "text", ""))
                return "\n".join([t for t in texts if t]).strip()
        except Exception:
            pass
        return ""

    # ---------------- Images ----------------
    def generate_image(self, prompt: str, model: Optional[str] = None) -> Tuple[bytes, str]:
        """
        Генерация изображения. Сначала пробуем выбранную модель, при ошибке — fallback на 'dall-e-3'.
        Возвращает (bytes, used_prompt).
        """
        if not self.enable_image_generation:
            raise RuntimeError("Генерация изображений отключена настройками.")

        primary = model or self.image_model or "dall-e-3"

        def _call(mdl: str) -> bytes:
            res = self.client.images.generate(
                model=mdl,
                prompt=prompt,
                size="1024x1024",
                response_format="b64_json",  # просим base64
            )
            try:
                data = res.data[0]
                b64 = getattr(data, "b64_json", None)
                if b64:
                    return base64.b64decode(b64)
                url = getattr(data, "url", None)
                if url:
                    r = requests.get(url, timeout=30)
                    r.raise_for_status()
                    return r.content
            except Exception as e:
                logger.warning("Images API parse failed: %s", e)
            raise RuntimeError("Images API did not return base64 image.")

        try:
            content = _call(primary)
            return content, prompt
        except Exception as e1:
            logger.warning("Primary image model '%s' failed: %s. Trying 'dall-e-3' fallback...", primary, e1)
            if primary != "dall-e-3":
                content = _call("dall-e-3")
                return content, prompt
            raise

    # ---------------- Web search ----------------
    def web_answer(self, query: str) -> Tuple[str, List[str]]:
        """
        Простой веб-поиск через html.duckduckgo.com с обработкой редиректов.
        Возвращает (ответ, [источники]).
        """
        url = "https://html.duckduckgo.com/html"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        }
        try:
            r = requests.get(url, params={"q": query}, headers=headers, allow_redirects=True, timeout=20)
            r.raise_for_status()
            html = r.text
            # Простейший парс ссылок
            links = re.findall(r'href="(https?://[^"]+)"', html)
            cleaned = []
            for u in links:
                if "duckduckgo.com" in u:
                    continue
                if u.startswith("https://r.duckduckgo.com/"):
                    continue
                cleaned.append(u)
            sources: List[str] = []
            for u in cleaned:
                if len(sources) >= 5:
                    break
                sources.append(u)

            ctx = ""
            if sources:
                ctx = "Ниже список источников:\n" + "\n".join(f"- {s}" for s in sources)

            ans = self.chat(
                f"Веб-запрос: {query}\n\n{ctx}\n\nСформируй краткий ответ и, если возможно, опирайся на источники.",
                model=self.default_model,
                temperature=self.default_temperature,
                style="Pro",
                kb_ctx=None,
            )
            return ans, sources
        except Exception as e:
            logger.exception("web search failed: %s", e)
            raise RuntimeError(f"Ошибка веб‑поиска: {_safe_str(e)}")

    # ---------------- Audio ----------------
    def transcribe_audio(self, audio_bytes: bytes) -> str:
        """
        Транскрипция через whisper-1.
        """
        f = io.BytesIO(audio_bytes)
        f.name = "audio.ogg"
        try:
            tr = self.client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
            if isinstance(tr, str):
                return tr.strip()
            txt = getattr(tr, "text", "")
            return (txt or "").strip()
        except Exception as e:
            logger.exception("transcribe_audio failed: %s", e)
            raise

    # ---------------- File/Image describe ----------------
    def describe_file(self, file_bytes: bytes, filename: str) -> str:
        h = hashlib.sha256(file_bytes).hexdigest()
        mime, _ = mimetypes.guess_type(filename or "")
        size_kb = len(file_bytes) // 1024
        prompt = (
            "Тебе передан файл (содержимое недоступно для анализа напрямую). "
            "Сформируй полезное резюме на основе названия и типа, предложи, что можно извлечь после индексации в БЗ. "
            f"Файл: {filename}, MIME: {mime}, Размер: ~{size_kb} KB, SHA256: {h[:16]}…"
        )
        return self.chat(prompt, self.default_model, self.default_temperature, "Pro", None)

    def describe_image(self, image_bytes: bytes) -> str:
        """
        Визуальное описание через Responses API с input_image (b64).
        """
        b64 = base64.b64encode(image_bytes).decode("ascii")
        try:
            rsp = self.client.responses.create(
                model="gpt-4o-mini",
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Опиши изображение кратко и по делу."},
                            {"type": "input_image", "image_data": {"data": b64, "mime_type": "image/png"}},
                        ],
                    }
                ],
                temperature=self.default_temperature,
            )
            text = self._first_text(rsp)
            return text or "(не удалось получить описание изображения)"
        except Exception as e:
            logger.exception("describe_image failed: %s", e)
            raise
