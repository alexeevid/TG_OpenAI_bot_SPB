from openai import OpenAI
from app import settings

class OpenAIClient:
    def __init__(self):
        self.client = OpenAI()

    def chat(self, messages, model=None):
        return self.client.chat.completions.create(
            model=model or settings.OPENAI_TEXT_MODEL,
            messages=messages
        ).choices[0].message.content

    def image(self, prompt: str):
        res = self.client.images.generate(
            model=settings.OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024"
        )
        return res.data[0].url

    def transcribe(self, file_obj):
        return self.client.audio.transcriptions.create(
            model=settings.OPENAI_TRANSCRIBE_MODEL,
            file=file_obj
        ).text