from __future__ import annotations

import base64
import io
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

try:
    # OpenAI SDK v1.x
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


def _json_list_from_env(name: str, default: Optional[List[str]] = None) -> List[str]:
    """
    Безопасный парсер JSON-массивов из переменных окружения.
    Допускает пустые/невалидные значения → возвращает default или [].
    """
    import json
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default or [])
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val]
        return list(default or [])
    except Exception:
        return list(default or [])


@dataclass
class GenImageResult:
    image_bytes: bytes
    final_prompt: str
    model_used: str


class OpenAIHelper:
    """
    Единая обёртка поверх OpenAI:
      - chat (Responses API)
      - transcribe (whisper-1)
      - image generation (gpt-image-1 / dall-e-3)
      - простой web_search (DuckDuckGo HTML с парсингом ссылок)
      - хранение текущей модели и стиля per-chat
    """

    def __init__(
        self,
        api_key: str,
        default_model: str = "gpt-4o",
        default_temperature: float = 0.2,
        image_primary: str = "gpt-image-1",
        image_fallback: str = "dall-e-3",
    ):
        if OpenAI is None:
            raise RuntimeError("OpenAI SDK не установлен.")
        self.client = OpenAI(api_key=api_key)
        self.default_model = default_model
        self.default_temperature = default_temperature
        self.image_primary = image_primary
        self.image_fallback = image_fallback

        # per-chat настройки
        self._per_chat_model: Dict[int, str] = {}
        self._per_chat_style: Dict[int, str] = {}  # pro|expert|user|ceo

    # ---------- MODELS ----------

    def list_models_with_current(self, chat_id: int) -> Tuple[List[str], str]:
        allow = _json_list_from_env("ALLOWED_MODELS_WHITELIST", [])
        deny = set(_json_list_from_env("DENYLIST_MODELS", []))
        current = self.get_current_model(chat_id)
        # если whitelist пуст — даём несколько адекватных дефолтов
        base = allow or ["gpt-4o", "gpt-4o-mini", "gpt-4", "o4-mini", "gpt-4.1-mini"]
        models = [m for m in base if m not in deny]
        return models, current

    def set_current_model(self, chat_id: int, model: str) -> None:
        self._per_chat_model[chat_id] = model

    def get_current_model(self, chat_id: int) -> str:
        return self._per_chat_model.get(chat_id, self.default_model)

    # ---------- STYLES ----------

    STYLES_SYS_PROMPT: Dict[str, str] = {
        "pro": (
            "Отвечай как высококвалифицированный профессионал с большим опытом. "
            "Пиши кратко, точно, выверенно, без воды, с ясной структурой."
        ),
        "expert": (
            "Отвечай как эксперт с глубокими знаниями предметной области. "
            "Давай развернутые объяснения, сравнения, аргументацию, упоминания методик и терминов."
        ),
        "user": (
            "Отвечай простым, человечным языком, избегай сложных терминов. "
            "Представь, что объясняешь другу-пользователю без спецподготовки."
        ),
        "ceo": (
            "Отвечай как собственник бизнеса уровня EMBA/DBA. "
            "Фокусируйся на стратегии, рисках, экономике и эффектах для бизнеса."
        ),
    }

    def set_style(self, chat_id: int, style: str) -> None:
        style = style.lower()
        if style not in self.STYLES_SYS_PROMPT:
            style = "pro"
        self._per_chat_style[chat_id] = style

    def get_style(self, chat_id: int) -> str:
        return self._per_chat_style.get(chat_id, "pro")

    # ---------- CHAT ----------

    def _compose_input(self, sys_hint: Optional[str], user_text: str) -> str:
        """
        Responses API принимает plain input. Формируем "префикс" для system.
        """
        if sys_hint:
            return f"[SYSTEM]\n{sys_hint}\n\n{user_text}"
        return user_text

    def chat(
        self,
        chat_id: int,
        text: str,
        kb_snippets: Optional[str] = None,
    ) -> str:
        """
        Генерация ответа. Если есть сниппеты БЗ — подмешиваем в system.
        """
        model = self.get_current_model(chat_id)
        style = self.get_style(chat_id)
        sys_hint = self.STYLES_SYS_PROMPT.get(style, "")

        if kb_snippets:
            sys_hint = (sys_hint + "\n\n"
                        "Учитывай следующие выдержки из документов базы знаний:\n"
                        f"{kb_snippets}").strip()

        prompt = self._compose_input(sys_hint, text)

        cc = self.client.responses.create(
            model=model,
            input=prompt,
            temperature=self.default_temperature,
        )
        # В SDK 1.x ответ лежит в content[0].text
        try:
            return cc.output_text  # у unified responses есть это свойство
        except Exception:
            pass

        try:
            # на всякий
            if cc and cc.output and len(cc.output) > 0:
                block = cc.output[0]
                if getattr(block, "content", None):
                    return "".join([getattr(p, "text", "") for p in block.content if getattr(p, "type", "") == "output_text"])
        except Exception:
            pass

        # более общий разбор
        try:
            if cc and cc.choices and len(cc.choices) > 0:
                msg = cc.choices[0].message
                if hasattr(msg, "content"):
                    return str(msg.content)
        except Exception:
            pass

        return "Не удалось получить ответ от модели."

    # ---------- TRANSCRIBE ----------

    def transcribe(self, audio_bytes: bytes, filename_hint: str = "audio.ogg") -> str:
        """
        Whisper-1 транскрипция.
        """
        # Записываем во временный файл, чтобы SDK корректно понял mimetype
        with tempfile.NamedTemporaryFile(suffix=os.path.splitext(filename_hint)[-1] or ".ogg") as f:
            f.write(audio_bytes)
            f.flush()
            f.seek(0)
            tr = self.client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
        # SDK 1.x может вернуть str
        return str(tr)

    # ---------- IMAGES ----------

    def generate_image(self, prompt: str, model: Optional[str] = None, size: str = "1024x1024") -> GenImageResult:
        """
        Возвращает bytes изображения + итоговый промпт + модель.
        Делает fallback и поддерживает как base64, так и URL.
        """
        primary = model or self.image_primary
        fallback = self.image_fallback

        def _call(m: str) -> Tuple[Optional[bytes], str]:
            res = self.client.images.generate(
                model=m,
                prompt=prompt,
                size=size,
            )
            if not res or not getattr(res, "data", None):
                return None, prompt

            item = res.data[0]
            # base64?
            b64 = getattr(item, "b64_json", None)
            if b64:
                return base64.b64decode(b64), prompt

            # url?
            url = getattr(item, "url", None)
            if url:
                bx = httpx.get(url, timeout=60.0)
                bx.raise_for_status()
                return bx.content, prompt

            return None, prompt

        try:
            img, pr = _call(primary)
            if img:
                return GenImageResult(img, pr, primary)
        except Exception:
            # пробуем fallback
            pass

        img, pr = _call(fallback)
        if not img:
            raise RuntimeError("Images API не вернуло изображение (ни base64, ни URL).")
        return GenImageResult(img, pr, fallback)

    # ---------- WEB SEARCH ----------

    def web_search(self, query: str, limit: int = 3) -> List[Dict[str, str]]:
        """
        Простая выдача ссылок через DuckDuckGo HTML без ключей.
        Парсинг по regex — без внешних зависимостей.
        """
        url = "https://duckduckgo.com/html"
        r = httpx.get(url, params={"q": query}, timeout=20.0)
        r.raise_for_status()
        html = r.text

        # Ищем блоки результатов <a class="result__a" href="...">Title</a>
        # DDG может менять разметку, поэтому держим это как best-effort
        pat = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
        results: List[Dict[str, str]] = []
        for m in pat.finditer(html):
            href = m.group(1)
            title = re.sub("<.*?>", "", m.group(2))  # вычищаем теги
            # Сниппет возьмём из соседнего result__snippet, если найдём
            # (упрощённо, без тяжёлых парсеров)
            snippet = ""
            # ограничимся ссылкой и заголовком
            results.append({"title": title.strip(), "url": href, "snippet": snippet})
            if len(results) >= limit:
                break
        return results
