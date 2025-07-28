from typing import List, Dict, Optional
from openai import OpenAI
import logging
from base64 import b64decode

logger = logging.getLogger(__name__)

class OpenAIHelper:
    """
    Обёртка над OpenAI:
    - list_models() — список моделей
    - set_model() — выбрать модель для текста
    - chat() — универсальный текстовый ответ (Responses API -> fallback Chat Completions)
    - generate_image() — генерация изображения (PNG как bytes)
    """
    def __init__(self, api_key: str, default_model: str, image_model: str = "gpt-image-1"):
        self.client = OpenAI(api_key=api_key)
        self.model = default_model
        self.image_model = image_model or "gpt-image-1"

    def list_models(self) -> List[str]:
        try:
            models = self.client.models.list()
            names = [m.id for m in models.data]
            names.sort()
            return names
        except Exception as e:
            logger.exception("Failed to list models: %s", e)
            # Резервный список, если API временно недоступен
            return ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini", "gpt-3.5-turbo"]

    def set_model(self, model: str):
        self.model = model

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
    ) -> str:
        """
        Сначала пробуем Responses API (поддерживает max_output_tokens),
        затем fallback на Chat Completions.
        """
        temperature = 0.3 if temperature is None else temperature
        max_output_tokens = max_output_tokens or 4096  # высокий потолок

        # Готовим input_text без f-строк с \n внутри выражений
        sys_texts = [m["content"] for m in messages if m["role"] == "system"]
        user_texts = [m["content"] for m in messages if m["role"] == "user"]
        sys_hint = "\n".join(sys_texts) if sys_texts else ""
        usr = user_texts[-1] if user_texts else ""
        prefix = "[SYSTEM]\n" + sys_hint + "\n\n" if sys_hint else ""
        input_text = prefix + usr

        # Responses API
        try:
            resp = self.client.responses.create(
                model=self.model,
                input=input_text,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
            text = getattr(resp, "output_text", "") or ""
            if text.strip():
                return text
        except Exception as e:
            logger.warning("Responses API failed, fallback to Chat Completions: %s", e)

        # Fallback: Chat Completions
        try:
            cc = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_output_tokens,
            )
            return cc.choices[0].message.content or ""
        except Exception as e:
            logger.exception("OpenAI chat failed: %s", e)
            raise

    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> bytes:
        """
        Генерирует изображение и возвращает PNG-байты.
        """
        try:
            res = self.client.images.generate(
                model=self.image_model,
                prompt=prompt,
                size=size,
            )
            b64 = res.data[0].b64_json
            return b64decode(b64)
        except Exception as e:
            logger.exception("Image generation failed: %s", e)
            raise
