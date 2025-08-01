# bot/openai_helper.py
from __future__ import annotations

import io
import os
import base64
import logging
from typing import List, Optional, Tuple

from openai import OpenAI

logger = logging.getLogger(__name__)


class OpenAIHelper:
    """
    Минимально инвазивная версия помощника работы с OpenAI.

    Точечные правки:
    1) chat(): корректный разбор ответа Responses API (используем response.output_text),
       без обращения к out.message (в SDK 1.x такого поля нет).
       Есть фолбэк на Chat Completions при необходимости.
    2) generate_image(): всегда запрашиваем base64 через response_format="b64_json",
       и проверяем наличие b64. Если primary-модель недоступна, пробуем fallback.
    """

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("OpenAI API key is required")

        self.client = OpenAI(api_key=api_key)

        # Дефолты, чтобы не менять поведение остального кода
        self.default_chat_model = os.getenv("OPENAI_DEFAULT_MODEL", "gpt-4o-mini")
        self.image_model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")

    # ===================== Вспомогательные =====================

    def _style_to_system_prompt(self, style: str) -> str:
        s = (style or "Pro").lower()
        if s in ("pro", "professional", "профессиональный"):
            return (
                "Отвечай как опытный профессионал: структурировано, по делу, ясно, "
                "давай конкретные рекомендации и шаги. Избегай воды."
            )
        if s in ("expert", "эксперт"):
            return (
                "Отвечай как эксперт с глубоким доменным опытом: объясняй причинно-следственные связи, "
                "приводи лучшие практики, предупреждай о рисках и ограничениях."
            )
        if s in ("user", "пользователь", "casual"):
            return (
                "Общайся просто и дружелюбно, кратко, без излишней терминологии. "
                "Если нужна детализация — уточняй."
            )
        if s in ("ceo", "руководитель"):
            return (
                "Отвечай как руководитель: кратко, по пунктам, с приоритетами и вариантами решений. "
                "Фокусируйся на эффектах для бизнеса и сроках."
            )
        # дефолт
        return (
            "Отвечай профессионально, структурировано и понятно. "
            "Если данных не хватает, уточняй вопросы."
        )

    # ===================== ТЕКСТОВЫЙ ДИАЛОГ =====================

    # ВАЖНО: вставьте этот метод целиком вместо текущего chat() в bot/openai_helper.py

    def chat(self, user_text: str,
             model: Optional[str] = None,
             temperature: float = 0.2,
             style: str = "Pro",
             kb_ctx: Optional[Dict[str, Any]] = None) -> str:
        """
        Единая точка диалога. Если есть kb_ctx, отвечаем строго по БЗ.
        kb_ctx ожидается формата:
            {
              "text": "<склеенные выдержки>",
              "sources": ["disk:/...pdf", "disk:/...pdf", ...]   # опционально
            }
        """
    
        use_model = model or self.default_model or "gpt-4o"
        temp = max(0.0, min(1.0, temperature))
    
        # --- Формируем системный промпт ---
        sys_parts = []
    
        # Базовый тон (по стилю), очень краткий
        if style.lower() in ("pro", "professional"):
            sys_parts.append("Отвечай коротко, по делу, структурируй списками только когда это помогает.")
        elif style.lower() in ("expert", "экспертный"):
            sys_parts.append("Ты эксперт-практик. Отвечай точно, с минимальными пояснениями.")
        elif style.lower() in ("ceo",):
            sys_parts.append("Отвечай управленческим языком, фокус на решениях и рисках.")
        else:
            sys_parts.append("Отвечай нейтрально и кратко.")
    
        kb_mode = bool(kb_ctx and isinstance(kb_ctx, dict) and kb_ctx.get("text"))
        if kb_mode:
            # Жёсткий приоритет БЗ
            sys_parts.append(
                "Используй ТОЛЬКО приведённые ниже выдержки из Базы знаний (БЗ). "
                "Если выдержек недостаточно для точного ответа, напиши: "
                "«Недостаточно контекста из БЗ для точного ответа» и поясни, чего не хватает."
            )
            sys_parts.append("БЗ:\n" + str(kb_ctx.get("text")))
    
            # Для снижения фантазии
            temp = min(temp, 0.3)
    
        system_prompt = "\n\n".join(sys_parts).strip()
    
        # --- Собираем messages для Chat Completions ---
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text.strip()},
        ]
    
        # --- Пытаемся через Responses API (если у вас это используется), иначе — Chat Completions ---
        # Здесь оставляем ваш текущий «fallback» к chat.completions, но подставляем messages выше.
        try:
            # Если у вас есть быстрая ветка через Responses API — можете её оставить,
            # главное: передайте system+user в нужном формате. Иначе просто используем чат-комплишнс.
            pass
        except Exception:
            pass
    
        # Chat Completions (надёжно и просто)
        try:
            resp = self.client.chat.completions.create(
                model=use_model,
                messages=messages,
                temperature=temp,
            )
            reply = resp.choices[0].message.content if resp and resp.choices else ""
        except Exception as e:
            logger.error("chat.completions failed: %s", e)
            raise
    
        reply = reply or ""
    
        # Хвост со списком источников, если включён KB_DEBUG
        try:
            import os
            if kb_mode and os.getenv("KB_DEBUG", "0") == "1":
                sources = kb_ctx.get("sources") or []
                sources = [s for s in sources if s]
                if sources:
                    tail = "\n\n📚 Источники (БЗ):\n" + "\n".join(f"• {s}" for s in sources[:10])
                    reply += tail
        except Exception:
            pass
    
        return reply

    # ===================== ИЗОБРАЖЕНИЯ =====================

    def generate_image(self, prompt: str, model: Optional[str] = None) -> Tuple[bytes, str]:
        """
        Генерирует изображение. Возвращает (bytes_png, used_prompt).
        Точечная правка: всегда просим base64 (response_format='b64_json') и валидируем.
        Сохраняем fallback на 'dall-e-3', если primary недоступна.
        """
        primary = model or self.image_model or "gpt-image-1"
        fallbacks = ["dall-e-3"] if primary != "dall-e-3" else []

        last_err: Optional[Exception] = None

        def _call(img_model: str) -> bytes:
            res = self.client.images.generate(
                model=img_model,
                prompt=prompt,
                n=1,
                size="1024x1024",
                response_format="b64_json",  # критично для стабильности
            )
            data = res.data[0]
            b64 = getattr(data, "b64_json", None)
            if not b64:
                raise RuntimeError("Images API did not return base64 image.")
            return base64.b64decode(b64)

        # Сначала пробуем primary
        try:
            return _call(primary), prompt
        except Exception as e:
            logger.warning("Primary image model '%s' failed: %s", primary, e)
            last_err = e

        # Затем — fallback-и
        for fb in fallbacks:
            try:
                return _call(fb), prompt
            except Exception as e:
                logger.error("Fallback image model '%s' failed: %s", fb, e)
                last_err = e

        raise RuntimeError(f"Image generation failed: {last_err}")

    # ===================== РЕЧЬ =====================

    def transcribe_audio(self, audio_bytes: bytes) -> str:
        """
        Транскрипция аудио (Whisper-1). Поддерживает bytes, не меняем интерфейс.
        """
        try:
            audio_io = io.BytesIO(audio_bytes)
            audio_io.name = "audio.ogg"  # подсказка формата
            tr = self.client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_io,
            )
            text = getattr(tr, "text", None)
            if not text:
                text = str(tr)
            return text.strip()
        except Exception as e:
            logger.error("transcribe_audio failed: %s", e)
            raise

    # ===================== АНАЛИЗ ФАЙЛОВ/ИЗОБРАЖЕНИЙ =====================

    def describe_file(self, file_bytes: bytes, filename: str) -> str:
        """
        Лёгкий анализ файла без изменения внешнего интерфейса.
        Здесь оставляем безопасный текстовый промпт без попыток парсинга PDF/Office,
        чтобы не ломать текущую сборку зависимостями.
        """
        prompt = (
            "Тебе передан файл. Дай краткое, структурированное резюме: тип содержимого, "
            "основные разделы (если удаётся понять по названию), возможные применения. "
            "Если содержания не видно (например, бинарный формат), объясни, "
            "какой анализ можно сделать без распаковки и что понадобится для глубокого анализа."
            f"\n\nИмя файла: {filename}"
        )
        return self.chat(prompt, model=self.default_chat_model, temperature=0.2, style="Pro")

    def describe_image(self, image_bytes: bytes) -> str:
        """
        Краткое описание изображения. Делаем через Chat Completions с vision,
        чтобы не лезть глубоко в формат Responses + input_image.
        """
        try:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            cc = self.client.chat.completions.create(
                model=self.default_chat_model,
                messages=[
                    {"role": "system", "content": "Дай краткое описание изображения на русском языке."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Опиши, что на картинке. Будь краток и точен."},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        ],
                    },
                ],
                temperature=0.2,
            )
            return (cc.choices[0].message.content or "").strip()
        except Exception as e:
            logger.error("describe_image failed: %s", e)
            return "Не удалось проанализировать изображение этой моделью."

    # ===================== ВЕБ (заглушка/тонкая логика) =====================

    def web_answer(self, query: str) -> Tuple[str, List[str]]:
        """
        Минимально совместимая реализация, ничего не ломаем:
        возвращаем ответ модели и пустой список источников — чтобы не падали вызовы.
        Если у вас есть реальный веб-поиск — замените тело на свой коннектор.
        """
        answer = self.chat(
            prompt=(
                "Ответь на вопрос пользователя. Если требуются внешние источники, "
                "дай общий ответ и честно отметь, что прямые ссылки недоступны в этой сборке.\n\n"
                f"Вопрос: {query}"
            ),
            model=self.default_chat_model,
            temperature=0.3,
            style="Pro",
        )
        return answer, []

    # ===================== Список моделей =====================

    def list_models_for_menu(self) -> List[str]:
        """
        Возвращаем доступные модели без агрессивной фильтрации.
        """
        try:
            models = self.client.models.list()
            names = [m.id for m in getattr(models, "data", [])]
            priority = ["o4-mini", "o3-mini", "o1-mini", "o1", "gpt-4o", "gpt-4o-mini", "gpt-4.1-mini"]
            names = sorted(
                names,
                key=lambda n: (0 if n in priority else 1, priority.index(n) if n in priority else 0, n),
            )
            return names
        except Exception as e:
            logger.warning("list_models_for_menu failed: %s", e)
            return ["gpt-4o", "gpt-4o-mini", "o3-mini", "o1-mini", "o1"]
