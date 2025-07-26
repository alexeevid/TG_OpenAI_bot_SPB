import logging
from openai import OpenAI

from bot.config import load_settings

logger = logging.getLogger(__name__)
settings = load_settings()


class OpenAIHelper:
    def __init__(self, api_key: str, model: str, image_model: str):
        self.api_key = api_key
        self.model = model
        self.image_model = image_model
        self.client = OpenAI(api_key=api_key)

    async def ask_chatgpt(self, messages, functions=None):
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                functions=functions,
                function_call="auto" if functions else None,
            )
            return response.choices[0].message
        except Exception as e:
            logger.error(f"Error in ask_chatgpt: {e}")
            return {"role": "assistant", "content": "Произошла ошибка при обращении к OpenAI."}

    async def generate_image(self, prompt: str) -> str:
        try:
            response = self.client.images.generate(
                model=self.image_model,
                prompt=prompt,
                size="1024x1024",
                quality="standard",
                n=1,
            )
            return response.data[0].url
        except Exception as e:
            logger.error(f"Image generation failed: {e}")
            return ""
