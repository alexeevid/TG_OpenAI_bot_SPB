import openai
import datetime
import tiktoken
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type
from typing import Dict

GPT_4O_MODELS = ("gpt-4o", "gpt-4o-mini", "chatgpt-4o-latest")

class OpenAIHelper:
    def __init__(self, config: Dict):
        self.config = config
        openai.api_key = config['api_key']
        self.client = openai.OpenAI(api_key=config['api_key'])
        self.conversations = {}
        self.last_updated = {}
        self.user_models = {}

    def reset_chat_history(self, chat_id: int):
        self.conversations[chat_id] = [{"role": "system", "content": "You are a helpful assistant."}]

    async def get_chat_response(self, chat_id: int, query: str):
        if chat_id not in self.conversations:
            self.reset_chat_history(chat_id)
        self.conversations[chat_id].append({"role":"user","content":query})
        resp = self.client.chat.completions.create(
            model=self.config['model'],
            messages=self.conversations[chat_id],
            temperature=0.3,
            max_tokens=1024
        )
        answer = resp.choices[0].message.content
        self.conversations[chat_id].append({"role":"assistant","content":answer})
        return answer, resp.usage.total_tokens

    async def generate_image(self, prompt: str):
        resp = self.client.images.generate(
            model=self.config['image_model'],
            prompt=prompt,
            size="1024x1024"
        )
        return resp.data[0].url, "1024x1024"
