# bot/openai_helper.py
from __future__ import annotations

import base64
import logging
import os
from typing import List, Optional, Tuple

from openai import OpenAI, APIError, BadRequestError  # type: ignore

logger = logging.getLogger(__name__)

# --- Маппинг ключей для передачи стиля ---
STYLE_MAP = {
    "Pro": "Профессионал",
    "Expert": "Эксперт",
    "User": "Пользователь",
    "CEO": "СЕО",
    # На всякий случай — русские тоже в себя
    "Профессионал": "Профессионал",
    "Эксперт": "Эксперт",
    "Пользователь": "Пользователь",
    "СЕО": "СЕО",
}

ROLE_SYSTEM_PROMPTS = {
    "Профессионал": (
        "Ты обязан отвечать исключительно как опытный профессионал: максимально кратко, ёмко и по существу. Не расписывай детали, не уходи в рассуждения. Покажи глубокое понимание вопроса, но используй минимум слов. Применяй списки, избегай “воды” и общих фраз."
    ),
    "Эксперт": (
        "Ты обязан отвечать как эксперт международного класса: системно, развернуто, строго по существу. Раскрывай суть через структуру, взаимосвязи и причины-следствия. Применяй профессиональные формулировки, используй точные определения и формируй целостную картину по теме, как если бы писал учебник или проводил мастер-класс. Не упрощай, не сокращай, показывай высокий уровень аналитики."
    ),
    "Пользователь": (
        "Ты обязан отвечать максимально простым, дружелюбным языком. Твоя задача — чтобы любой человек без специальной подготовки понял ответ. Объясняй понятия через аналогии и примеры из жизни, избегай любых профессиональных терминов. Добавляй побочные пояснения и необходимые ответвления, чтобы раскрыть контекст и сделать материал интуитивно понятным для обычного пользователя."
    ),
    "СЕО": (
        "Ты обязан отвечать с точки зрения генерального директора компании (100–500 человек, 7–10 лет на рынке). Всегда исходи из стратегических целей, влияния на бизнес, финансовых последствий и предпринимательских рисков. Не вдавайся в детали процессов и методологий. Показывай только влияние на компанию, людей, прибыль, устойчивость бизнеса и рыночное положение. Формулируй выводы как бизнес-лидер для совета директоров или собственников."
    ),
}

FEW_SHOT_EXAMPLES = {
    "Профессионал": [
        {"role": "user", "content": "Что такое жизненный цикл проекта?"},
        {"role": "assistant", "content": (
            "Жизненный цикл проекта — это 5 последовательных фаз: инициация, планирование, исполнение, контроль, завершение. Каждая фаза — это логический этап с конкретными задачами и целями. Итог: проект переходит от идеи к завершённому результату."
        )},
    ],
    "Эксперт": [
        {"role": "user", "content": "Что такое жизненный цикл проекта?"},
        {"role": "assistant", "content": (
            "Жизненный цикл проекта — это системная модель, описывающая стадии развития проекта от инициации до закрытия. Классическая структура: инициация (формализация целей и допуск), планирование (детализация работ, ресурсное обеспечение, матрицы ответственности), исполнение (реализация плана, управление командой), мониторинг и контроль (оценка показателей, корректировка отклонений), завершение (формальное закрытие, анализ полученного опыта). В международной практике различают жизненные циклы предсказуемого (Waterfall) и адаптивного (Agile) типов. Корректное определение фаз — ключевой элемент зрелого управления проектами."
        )},
    ],
    "Пользователь": [
        {"role": "user", "content": "Что такое жизненный цикл проекта?"},
        {"role": "assistant", "content": (
            "Жизненный цикл проекта — это как путь от задумки до результата. Например, если вы решили сделать ремонт в квартире, сначала придумываете, что хотите (инициация), потом планируете этапы и что нужно купить (планирование), дальше ремонтируете (исполнение), проверяете, что всё идёт по плану (контроль), и в конце наводите порядок и радуетесь результату (завершение). Всё просто!"
        )},
    ],
    "СЕО": [
        {"role": "user", "content": "Что такое жизненный цикл проекта?"},
        {"role": "assistant", "content": (
            "Как CEO я рассматриваю жизненный цикл проекта исключительно через призму влияния на компанию: проект стартует, если обеспечивает рост, прибыль или стратегическое преимущество. Главное — чёткая постановка целей, понимание ключевых точек контроля, эффективное распределение ресурсов и быстрый вывод результата на рынок. Любая задержка или неясность — это прямой риск для бизнеса."
        )},
    ],
}

class OpenAIHelper:
    """
    Обёртка над OpenAI SDK с единым интерфейсом для бота.
    """

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("OpenAI API key is required")
        self._client = OpenAI(api_key=api_key)
        self.default_chat_model = os.getenv("OPENAI_MODEL", "gpt-4o")
        self.default_image_model = os.getenv("IMAGE_MODEL", "gpt-image-1")
        self.default_embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        self._whitelist_env = os.getenv("ALLOWED_MODELS_WHITELIST", "")

    def list_models_for_menu(self) -> List[str]:
        if self._whitelist_env.strip():
            items = [m.strip() for m in self._whitelist_env.split(",") if m.strip()]
            return items or [self.default_chat_model]
        return [
            "gpt-4o",
            "gpt-4o-mini",
            "o4-mini",
            "o4",
        ]

    def chat(
        self,
        user_text: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        style: str = "Pro",
        kb_ctx: Optional[str] = None,
    ) -> str:
        chat_model = model or self.default_chat_model

        # --- Маппинг стиля на правильный ключ словаря ---
        mapped_style = STYLE_MAP.get(style, style)

        if kb_ctx:
            messages = [
                {
                    "role": "system",
                    "content": "Ты помощник, который отвечает на вопросы пользователя, используя нижеуказанный контекст из базы знаний. Если в контексте нет ответа, честно скажи, что не знаешь."
                },
                {
                    "role": "user",
                    "content": f"Контекст:\n{kb_ctx}"
                },
                {
                    "role": "user",
                    "content": user_text
                },
            ]
        else:
            system_text = ROLE_SYSTEM_PROMPTS.get(mapped_style)
            if not system_text:
                system_text = "You are a helpful assistant. Answer concisely and clearly."
            few_shot = FEW_SHOT_EXAMPLES.get(mapped_style, [])
            messages = [{"role": "system", "content": system_text}]
            messages.extend(few_shot)
            messages.append({"role": "user", "content": user_text})

        logger.debug("PROMPT to OpenAI:\n%s", messages)

        try:
            resp = self._client.chat.completions.create(
                model=chat_model,
                messages=messages,
                temperature=float(temperature or 0.2),
            )
            out = (resp.choices[0].message.content or "").strip()
            if out:
                return out
        except BadRequestError as e:
            logger.error("chat() BadRequest: %s", e)
        except APIError as e:
            logger.error("chat() APIError: %s", e)
        except Exception as e:  # pragma: no cover
            logger.error("chat() failed: %s", e)

        return "Извините, не удалось получить ответ от модели."

    # --- Изображения ---

    def generate_image(self, prompt: str, size: Optional[str] = None) -> Tuple[bytes, str]:
        model = self.default_image_model
        img_size = size or "1024x1024"
        try:
            res = self._client.images.generate(
                model=model,
                prompt=prompt,
                size=img_size,
            )
            b64 = res.data[0].b64_json
            png_bytes = base64.b64decode(b64) if b64 else b""
            return png_bytes, prompt
        except Exception as e:
            logger.error("generate_image() failed: %s", e)
            raise

    # --- Аудио ---

    def transcribe_audio(self, audio_bytes: bytes) -> str:
        try:
            tmp = "/tmp/voice.ogg"
            with open(tmp, "wb") as f:
                f.write(audio_bytes)
            with open(tmp, "rb") as f:
                tr = self._client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                )
            text = getattr(tr, "text", "") or ""
            return text.strip()
        except Exception as e:
            logger.error("transcribe_audio() failed: %s", e)
            raise

    # --- Простые описатели для вложений ---

    def describe_file(self, file_bytes: bytes, filename: str) -> str:
        name = filename or "file"
        size_kb = len(file_bytes) // 1024
        return f"Файл: {name}, размер ~{size_kb} KB. Поддержка глубокой семантической выжимки не включена."

    def describe_image(self, image_bytes: bytes) -> str:
        size_kb = len(image_bytes) // 1024
        return f"Изображение получено ({size_kb} KB). Анализ изображений в этой сборке минимален."

    # --- Веб-поиск (заглушка) ---

    def web_answer(self, query: str) -> Tuple[str, List[str]]:
        answer = (
            "В этой сборке веб-поиск отключён. "
            "Могу ответить на основании вашего контекста/БЗ, если он включён."
        )
        return answer, []
