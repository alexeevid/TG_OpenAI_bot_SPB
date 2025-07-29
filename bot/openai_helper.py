# bot/openai_helper.py
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse, urljoin, unquote

import httpx
from openai import OpenAI

logger = logging.getLogger(__name__)


# ----------------------------- Вспомогательные типы ---------------------------

@dataclass
class SettingsLike:
    """Мини-интерфейс для настроек. Реальный Settings может содержать больше полей."""
    openai_api_key: str
    openai_model: Optional[str] = None
    image_model: Optional[str] = None
    openai_temperature: float = 0.2
    enable_image_generation: bool = True
    allowed_models_whitelist: Optional[List[str]] = None
    denylist_models: Optional[List[str]] = None


# ------------------------------- Основной хелпер ------------------------------

class OpenAIHelper:
    """
    Утилита работы с OpenAI и внешними функциями (поиск, транскрибация, т.п.)
    Совместима с openai==1.x.

    Важно: чат реализован через Chat Completions API, чтобы избежать ошибок Responses API
    вида 'got an unexpected keyword argument "messages"'.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        image_model: Optional[str] = None,
        temperature: float = 0.2,
        enable_image_generation: bool = True,
        settings: Optional[SettingsLike] = None,
    ) -> None:
        # Источник параметров: явные аргументы имеют приоритет, затем settings, затем ENV
        if settings is not None and not api_key:
            api_key = getattr(settings, "openai_api_key", None)

        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))

        self.default_model: str = (
            default_model
            or (getattr(settings, "openai_model", None) if settings else None)
            or os.getenv("OPENAI_MODEL")
            or "gpt-4o"
        )
        self.default_image_model: str = (
            image_model
            or (getattr(settings, "image_model", None) if settings else None)
            or os.getenv("IMAGE_MODEL")
            or "dall-e-3"
        )
        self.temperature: float = (
            temperature
            if temperature is not None
            else (getattr(settings, "openai_temperature", 0.2) if settings else 0.2)
        )
        self.enable_image_generation: bool = (
            enable_image_generation
            if enable_image_generation is not None
            else bool(getattr(settings, "enable_image_generation", True) if settings else True)
        )

        self.allowed_models_whitelist: Optional[List[str]] = (
            getattr(settings, "allowed_models_whitelist", None) if settings else None
        )
        self.denylist_models: Optional[List[str]] = (
            getattr(settings, "denylist_models", None) if settings else None
        )

        # Пользовательские модели на уровне пользователя (переключение через /model)
        self._user_models: Dict[int, str] = {}

    # ------------------------------- Модели -----------------------------------

    def list_models_for_user(self, user_id: int) -> List[str]:
        """Вернёт список моделей с учётом whitelist/denylist (если заданы)."""
        try:
            models = [m.id for m in self.client.models.list().data]
        except Exception as e:
            logger.warning("Не удалось получить список моделей у OpenAI: %s", e)
            # Фолбэк на разумный набор
            models = [
                "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini",
                "o3-mini", "o1-mini", "o4-mini",
            ]

        # Фильтруем по whitelist/denylist
        if self.allowed_models_whitelist:
            allowed_set = set(self.allowed_models_whitelist)
            models = [m for m in models if m in allowed_set]
        if self.denylist_models:
            deny = set(self.denylist_models)
            models = [m for m in models if m not in deny]

        # Оставляем только чатовые/универсальные
        keep_keywords = ("gpt", "o1", "o3", "o4")
        models = [m for m in models if any(k in m for k in keep_keywords)]

        # Упорядочим поверхностно (чтобы дефолтная оказалась первой)
        def _score(mid: str) -> int:
            return 0 if mid == self.default_model else 1
        models.sort(key=_score)

        # И уберём дубликаты, сохраняя порядок
        seen = set()
        unique: List[str] = []
        for m in models:
            if m not in seen:
                unique.append(m)
                seen.add(m)
        return unique[:40]  # не раздуваем инлайн‑клавиатуру

    def set_user_model(self, user_id: int, model: str) -> None:
        self._user_models[user_id] = model

    def get_user_model(self, user_id: int) -> Optional[str]:
        return self._user_models.get(user_id)

    def get_image_model(self) -> str:
        return self.default_image_model

    # -------------------------------- Чат ------------------------------------

    def _style_to_system(self, style: str) -> str:
        style = (style or "Pro").lower()
        if style == "pro":
            return (
                "Отвечай как высококвалифицированный профессионал. Пиши кратко, точно, "
                "по делу, с акцентом на практические выводы и корректные термины."
            )
        if style == "expert":
            return (
                "Отвечай как эксперт с глубокими знаниями в теме. Объясняй детали, "
                "приводи аргументацию, указывай ограничения и нюансы."
            )
        if style == "user":
            return (
                "Отвечай просто и понятно, как обычный опытный пользователь. "
                "Избегай перегруза терминами, делай ответ дружелюбным."
            )
        if style == "ceo":
            return (
                "Отвечай голосом собственника бизнеса (уровень EMBA/DBA): акцент на стратегию, "
                "ценность, риски, метрики, влияние на прибыль и организацию."
            )
        return "Будь полезным и точным."

    def chat(
        self,
        user_id: int,
        dialog_id: int,
        user_text: str,
        model: Optional[str] = None,
        style: str = "Pro",
        kb: Optional[Iterable[Tuple[int, str]]] = None,
    ) -> str:
        """
        Основной диалог. Если переданы kb, добавляет выдержки в системный контекст.
        """
        use_model = model or self.get_user_model(user_id) or self.default_model

        system_parts = [self._style_to_system(style)]
        if kb:
            # Сформируем короткий контекст из БЗ (ограничим размер)
            ctx_parts: List[str] = []
            budget = 2500  # символов на БЗ
            for _, chunk in kb:
                if budget <= 0:
                    break
                add = chunk[: min(len(chunk), budget)]
                ctx_parts.append(add)
                budget -= len(add)
            kb_text = "\n\n".join(ctx_parts)
            if kb_text:
                system_parts.append(
                    "Используй приведённые выдержки из базы знаний как главный источник фактов. "
                    "Если информация отсутствует — отвечай по общим знаниям, но явно пометь это."
                )
                system_parts.append(f"Выдержки БЗ:\n{kb_text}")

        system_prompt = "\n\n".join(system_parts)

        try:
            resp = self.client.chat.completions.create(
                model=use_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=self.temperature,
            )
            return resp.choices[0].message.content or "…"
        except Exception as e:
            logger.exception("Chat error: %s", e)
            raise

    # ---------------------------- Генерация изображений ----------------------

    def _image_b64_or_raise(self, res) -> bytes:
        """
        Приводим ответ Images API к bytes. Требуем b64_json.
        """
        try:
            data0 = res.data[0]
        except Exception:
            raise RuntimeError("Images API returned empty response.")

        b64 = getattr(data0, "b64_json", None)
        if not b64:
            # Иногда возвращают url, но мы принудительно просим b64_json — если его нет, считаем ошибкой
            raise RuntimeError("Images API did not return base64 image.")
        return base64.b64decode(b64)

    def generate_image(self, prompt: str, model: Optional[str] = None) -> Tuple[bytes, str]:
        """
        Возвращает (image_bytes, used_prompt). Делает попытку на primary модели, затем fallback.
        """
        if not self.enable_image_generation:
            raise RuntimeError("Генерация изображений отключена настройками.")

        primary = model or self.default_image_model or "dall-e-3"
        fallbacks = ["gpt-image-1", "dall-e-3"] if primary != "gpt-image-1" else ["dall-e-3"]

        def _call(m: str) -> bytes:
            res = self.client.images.generate(
                model=m,
                prompt=prompt,
                size="1024x1024",
                response_format="b64_json",
                quality="standard",
            )
            return self._image_b64_or_raise(res)

        # 1) primary
        try:
            return _call(primary), prompt
        except Exception as e1:
            logger.warning("Primary image model '%s' failed: %s. Trying fallback...", primary, e1)

        # 2) fallbacks
        last_err: Optional[Exception] = None
        for fb in fallbacks:
            try:
                return _call(fb), prompt
            except Exception as e2:
                last_err = e2
                logger.error("Fallback image model '%s' failed: %s", fb, e2)

        raise RuntimeError(str(last_err) if last_err else "Image generation failed.")

    # --------------------------------- Веб‑поиск ------------------------------

    _DDG_HTML = "https://duckduckgo.com/html/"

    def _extract_ddg_results(self, html: str) -> List[Tuple[str, str, str]]:
        """Парсинг простого HTML DDG: (title, url, snippet)."""
        results: List[Tuple[str, str, str]] = []
        # Результаты в ссылках вида <a class="result__a" href="/l/?kh=...&uddg=<ENC_URL>">Title</a>
        # Возьмём пары блоков заголовок+сниппет
        # Заголовок:
        for blk in re.findall(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            html,
            flags=re.S | re.I,
        ):
            raw_href, raw_title, raw_snippet = blk
            # Декодируем uddg
            url = raw_href
            try:
                parsed = urlparse(raw_href)
                qs = parse_qs(parsed.query)
                uddg = qs.get("uddg", [""])[0]
                if uddg:
                    url = unquote(uddg)
                else:
                    # если сразу абсолютная ссылка
                    if raw_href.startswith("/"):
                        url = urljoin(self._DDG_HTML, raw_href)
                    else:
                        url = raw_href
            except Exception:
                url = raw_href

            # Уберём HTML-теги в тексте
            title = re.sub(r"<[^>]+>", "", raw_title).strip()
            snippet = re.sub(r"<[^>]+>", "", raw_snippet).strip()
            if title and url:
                results.append((title, url, snippet))
            if len(results) >= 8:
                break
        return results

    def web_search(self, query: str) -> Tuple[str, List[str]]:
        """
        Возвращает (краткий ответ, список ссылок).
        Мы не обходим сайты, используем сниппеты DDG и просим модель сделать ответ.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        }
        try:
            with httpx.Client(follow_redirects=True, timeout=15.0, headers=headers) as cli:
                r = cli.get(self._DDG_HTML, params={"q": query})
                r.raise_for_status()
                results = self._extract_ddg_results(r.text)
        except httpx.HTTPStatusError as e:
            logger.exception("HTTP error DDG: %s", e)
            raise RuntimeError(str(e))
        except Exception as e:
            logger.exception("DDG fetch error: %s", e)
            raise RuntimeError(str(e))

        links = [u for _, u, _ in results][:8]
        # Подготовим контекст для модели
        snippets_text = "\n\n".join([f"{i+1}. {t}\n{u}\n{s}" for i, (t, u, s) in enumerate(results[:6])])
        sys = (
            "Ты помощник с функцией веб‑поиска. На основе предоставленных сниппетов поисковой выдачи "
            "сформируй краткий и точный ответ на запрос пользователя. Когда уместно, упоминай источники "
            "(достаточно списка ссылок в конце). Если сниппеты не отвечают на вопрос — скажи об этом."
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.default_model,
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user", "content": f"Запрос: {query}\n\nСниппеты:\n{snippets_text}"},
                ],
                temperature=min(self.temperature, 0.5),
            )
            answer = resp.choices[0].message.content or ""
        except Exception as e:
            logger.exception("OpenAI summarize for web failed: %s", e)
            answer = "Не удалось сформировать сводку по результатам поиска."

        return answer, links

    # --------------------------------- STT / TTS ------------------------------

    def transcribe(self, file_path: str, model: Optional[str] = None) -> str:
        """
        Транскрибация аудио. По умолчанию — whisper-1.
        """
        use_model = model or os.getenv("STT_MODEL") or "whisper-1"
        try:
            with open(file_path, "rb") as f:
                res = self.client.audio.transcriptions.create(
                    model=use_model,
                    file=f,
                )
            # openai==1.x возвращает text в res.text (или res.text в data)
            text = getattr(res, "text", None) or getattr(res, "text", "")
            if not text and hasattr(res, "segments"):
                # на всякий — собрать сегменты
                text = " ".join(getattr(seg, "text", "") for seg in res.segments or [])
            return text or ""
        except Exception as e:
            logger.exception("Transcribe failed: %s", e)
            raise

    # ------------------------------ Анализ файлов -----------------------------

    def describe_file(self, file_path: str) -> str:
        """
        Быстрый анализ файла для ответа в чате:
        - Для изображений используем vision (base64 Data URL + короткое описание).
        - Для прочих типов даём краткую справку.
        """
        try:
            size = os.path.getsize(file_path)
        except Exception:
            size = 0
        name = os.path.basename(file_path)
        mime, _ = mimetypes.guess_type(file_path)
        mime = mime or "application/octet-stream"

        # Изображение — подключим vision
        if mime.startswith("image/"):
            try:
                with open(file_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                data_url = f"data:{mime};base64,{b64}"
                resp = self.client.chat.completions.create(
                    model=self.default_model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Дай краткое описание изображения и ключевые детали."},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }],
                    temperature=0.2,
                )
                desc = resp.choices[0].message.content or ""
            except Exception as e:
                logger.warning("Vision describe failed: %s", e)
                desc = "Не удалось распознать изображение."
            return f"Файл: {name}\nТип: {mime}\nРазмер: {size} байт\n\n{desc}"

        # Для документов пока без извлечения текста — дадим аккуратное резюме
        return (
            f"Файл: {name}\n"
            f"Тип: {mime}\n"
            f"Размер: {size} байт\n\n"
            "Краткий анализ: файл получен. Подробное извлечение текста и индексация доступны через команду Базы знаний."
        )
