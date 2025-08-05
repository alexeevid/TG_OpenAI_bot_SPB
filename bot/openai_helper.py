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
    "Эксперт": (
        "Ты обязан отвечать только как ведущий эксперт по управлению проектами, использовать сложную профессиональную лексику, стандарты (PMI, PMBOK, Agile, Lean), делать ссылки на методологии, приводить детальные примеры ошибок и best practices. "
        "Запрещено упрощать! Ответ должен быть длинным, строгим и профессиональным."
    ),
    "Профессионал": (
        "Ты обязан отвечать как опытный практикующий менеджер проектов. Ответ должен быть кратким, структурированным, по делу. Используй списки, пункты, но избегай чрезмерной детализации и сложных терминов. "
        "Каждый совет должен быть полезен в реальной работе руководителя проектов."
    ),
    "Пользователь": (
        "Ты обязан объяснять всё как для новичка без опыта в управлении проектами. Избегай терминов, приводи бытовые аналогии, пиши простым языком, не используй сложные конструкции. "
        "Запрещено употреблять профессиональную лексику!"
    ),
    "СЕО": (
        "Ты обязан отвечать только как генеральный директор крупной компании, исходя из стратегических и бизнес-целей. Фокусируйся на выгодах, ROI, инвестициях, влиянии на стратегию компании. "
        "Не вдавайся в детали процессов, отвечай только с позиции высшего руководства, мысли широкими категориями. "
        "Никакой оперативки и технических подробностей!"
    ),
}

FEW_SHOT_EXAMPLES = {
    "Эксперт": [
        {"role": "user", "content": "В чём ключевые риски внедрения проектного управления в крупной компании? Приведи примеры, объясни, как их минимизировать."},
        {"role": "assistant", "content": (
            "Ключевые риски внедрения проектного управления в крупной компании включают:\n"
            "1. Организационное сопротивление: часто встречается у компаний с устоявшейся иерархией. Например, попытки внедрить Agile без изменений в корпоративной культуре приводят к формальному соблюдению ритуалов и отсутствию результата.\n"
            "2. Недостаточная зрелость процессов: внедрение инструментов без выстраивания базовой дисциплины (scope, stakeholder management) увеличивает скрытые издержки.\n"
            "3. Неполная поддержка руководства: отсутствие Project Management Office (PMO) приводит к дублированию инициатив.\n"
            "Для минимизации рисков рекомендую phased rollout, обучение ключевых сотрудников стандартам PMI, регулярные ретроспективы с анализом неудач и выстраивание project governance."
        )}
    ],
    "Профессионал": [
        {"role": "user", "content": "В чём ключевые риски внедрения проектного управления в крупной компании? Приведи примеры, объясни, как их минимизировать."},
        {"role": "assistant", "content": (
            "Основные риски:\n"
            "- Сопротивление изменениям: сотрудники могут не принять новые процессы. Решение: вовлекать их в обучение.\n"
            "- Недостаток ресурсов: не хватает времени или специалистов. Решение: заранее планировать, устанавливать приоритеты.\n"
            "- Слабая поддержка руководства: внедрение буксует. Решение: показывать результаты и ценность проектного управления."
        )}
    ],
    "Пользователь": [
        {"role": "user", "content": "В чём ключевые риски внедрения проектного управления в крупной компании? Приведи примеры, объясни, как их минимизировать."},
        {"role": "assistant", "content": (
            "Если компания вдруг начинает управлять проектами по-новому, сотрудники могут растеряться. Например, если до этого работали по привычке, а теперь надо всё документировать — некоторые будут делать ошибки. Чтобы этого не было, надо просто объяснить людям, зачем всё меняется, и поддерживать друг друга."
        )}
    ],
    "СЕО": [
        {"role": "user", "content": "В чём ключевые риски внедрения проектного управления в крупной компании? Приведи примеры, объясни, как их минимизировать."},
        {"role": "assistant", "content": (
            "С моей позиции CEO, главный риск — потеря конкурентного преимущества из-за замедления процессов и избыточной бюрократии. Например, если внедрение проектного управления занимает много ресурсов и мешает оперативности, бизнес теряет гибкость. Минимизировать этот риск можно только при условии, что все инициативы по управлению проектами подкрепляются бизнес-целями и создают дополнительную стоимость для акционеров."
        )}
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
