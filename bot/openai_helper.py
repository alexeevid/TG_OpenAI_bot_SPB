import logging
import asyncio
from typing import List, Optional, Dict, Any
from openai import OpenAI
from .db.session import SessionLocal
from .db.models import Message
from .dialog_manager import DialogManager

logger = logging.getLogger(__name__)

# System prompts для ролей
ROLE_SYSTEM_PROMPTS = {
    "Pro": "Вы — профессиональный ассистент. Отвечайте кратко, по делу и строго по теме.",
    "Expert": "Вы — эксперт в своей области. Даёте развёрнутые, подробные и технически точные ответы.",
    "User": "Вы — дружелюбный помощник, отвечаете простым языком.",
    "CEO": "Вы — генеральный директор, который формулирует стратегические и управленческие ответы."
}

class OpenAIHelper:
    def __init__(self, api_key: str, model: str, image_model: str):
        self.api_key = api_key
        self.model = model
        self.image_model = image_model
        self._client = OpenAI(api_key=self.api_key)
        self.dialog_manager = DialogManager()

    async def chat(
        self,
        dialog_id: Optional[int],
        user_id: Optional[int],
        user_message: str,
        style: Optional[str] = None,
        kb_context: Optional[str] = None,
        model: Optional[str] = None
    ) -> str:
        """
        Генерация ответа с учётом истории диалога и контекста из KB.
        """
        messages = []

        # 1. Добавляем system prompt по стилю
        role_prompt = ROLE_SYSTEM_PROMPTS.get(style or "User", ROLE_SYSTEM_PROMPTS["User"])
        messages.append({"role": "system", "content": role_prompt})

        # 2. Если есть KB-контекст — добавляем его как отдельный system
        if kb_context:
            kb_intro = "Используй следующий контекст из базы знаний при ответе:\n" + kb_context
            messages.append({"role": "system", "content": kb_intro})

        # 3. Добавляем историю из БД
        if dialog_id and user_id:
            history_messages = self.dialog_manager.get_messages(dialog_id, limit=8)
            for msg in reversed(history_messages):  # chronological
                messages.append({"role": msg.role, "content": msg.content})

        # 4. Добавляем текущее сообщение пользователя
        messages.append({"role": "user", "content": user_message})

        # Логируем первые 300 символов prompt для отладки
        debug_preview = str(messages)[:300]
        logger.debug(f"Prompt[:300]: {debug_preview}")

        # 5. Вызов OpenAI API
        chosen_model = model or self.model
        try:
            completion = await asyncio.to_thread(
                lambda: self._client.chat.completions.create(
                    model=chosen_model,
                    messages=messages
                )
            )
            reply = completion.choices[0].message.content.strip()
            return reply
        except Exception as e:
            logger.error(f"Ошибка OpenAI Chat API: {e}", exc_info=True)
            return f"Ошибка обращения к OpenAI: {e}"

    async def generate_image(self, prompt: str) -> Optional[bytes]:
        """
        Генерация изображения через OpenAI.
        """
        try:
            result = await asyncio.to_thread(
                lambda: self._client.images.generate(
                    model=self.image_model,
                    prompt=prompt,
                    size="1024x1024"
                )
            )
            image_url = result.data[0].url
            import requests
            img_data = requests.get(image_url).content
            return img_data
        except Exception as e:
            logger.error(f"Ошибка генерации изображения: {e}", exc_info=True)
            return None

    async def transcribe_audio(self, audio_path: str) -> str:
        """
        Распознавание аудио.
        """
        try:
            with open(audio_path, "rb") as audio_file:
                transcript = await asyncio.to_thread(
                    lambda: self._client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file
                    )
                )
            return transcript.text
        except Exception as e:
            logger.error(f"Ошибка транскрипции аудио: {e}", exc_info=True)
            return f"[Ошибка транскрипции: {e}]"

    async def describe_file(self, file_path: str) -> str:
        """
        Генерация описания файла (заглушка).
        """
        return f"Описание файла: {file_path}"

    async def describe_image(self, image_path: str) -> str:
        """
        Генерация описания изображения (заглушка).
        """
        return f"Описание изображения: {image_path}"

    def list_models_for_menu(self) -> List[str]:
        """
        Получение списка доступных моделей (заглушка).
        """
        return ["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo"]
