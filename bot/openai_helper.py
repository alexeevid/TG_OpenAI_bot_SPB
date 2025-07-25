
import openai
import httpx
import logging
from typing import List, Dict, Any
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type
from openai import OpenAI

class OpenAIHelper:
    def __init__(self, api_key: str, model: str, image_model: str):
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.image_model = image_model

    @retry(reraise=True, retry=retry_if_exception_type(Exception), wait=wait_fixed(2), stop=stop_after_attempt(3))
    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.2, max_tokens: int = 1024) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens
        )
        return resp.choices[0].message.content

    def generate_image(self, prompt: str, size: str = "1024x1024") -> str:
        resp = self.client.images.generate(
            model=self.image_model,
            prompt=prompt,
            size=size,
            n=1
        )
        return resp.data[0].url
