
import datetime
import logging
import json
import io
from typing import Tuple, Dict, Any, List

import openai
import httpx
import tiktoken
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

GPT_O_MODELS = ("o1", "o1-mini", "o1-preview")
GPT_4O_MODELS = ("gpt-4o", "gpt-4o-mini", "chatgpt-4o-latest")

def default_max_tokens(model: str) -> int:
    return 4096

def are_functions_available(model: str) -> bool:
    return model not in GPT_O_MODELS

class OpenAIHelper:
    def __init__(self, config: dict, plugin_manager):
        self.config = config
        self.config.setdefault("embedding_model", "text-embedding-3-small")
        self.plugin_manager = plugin_manager
        http_client = httpx.AsyncClient(proxy=config.get('proxy')) if 'proxy' in config else None
        self.client = openai.AsyncOpenAI(api_key=config['api_key'], http_client=http_client)

        self.conversations: Dict[int, list] = {}
        self.last_updated: Dict[int, datetime.datetime] = {}
        self.user_models: Dict[int, str] = {}
        self.conversations_vision: Dict[int, bool] = {}

    def reset_chat_history(self, chat_id: int, content=''):
        if not content:
            content = self.config['assistant_prompt']
        role = "assistant" if self.config['model'] in GPT_O_MODELS else "system"
        self.conversations[chat_id] = [{"role": role, "content": content}]
        self.conversations_vision[chat_id] = False

    def get_conversation_stats(self, chat_id: int) -> Tuple[int, int]:
        if chat_id not in self.conversations:
            self.reset_chat_history(chat_id)
        return len(self.conversations[chat_id]), self._count_tokens(self.conversations[chat_id])

    async def get_chat_response(self, chat_id: int, query: str) -> Tuple[str, str]:
        response = await self._common_get_chat_response(chat_id, query)
        answer = response.choices[0].message.content.strip()
        self._add_to_history(chat_id, "assistant", answer)
        return answer, str(response.usage.total_tokens)

    async def get_chat_response_stream(self, chat_id: int, query: str):
        response = await self._common_get_chat_response(chat_id, query, stream=True)
        answer = ''
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                answer += delta.content
                yield answer, 'not_finished'
        self._add_to_history(chat_id, "assistant", answer)
        yield answer, str(self._count_tokens(self.conversations[chat_id]))

    async def generate_image(self, prompt: str):
        try:
            resp = await self.client.images.generate(
                model=self.config['image_model'],
                prompt=prompt,
                n=1,
                size=self.config['image_size']
            )
            if not resp.data:
                raise RuntimeError("Empty response from image API")
            return resp.data[0].url, self.config['image_size']
        except Exception as e:
            raise Exception(f"Ошибка генерации изображения: {e}")

    async def transcribe(self, filename: str) -> str:
        with open(filename, "rb") as audio:
            result = await self.client.audio.transcriptions.create(model="whisper-1", file=audio, prompt=self.config['whisper_prompt'])
            return result.text

    async def generate_speech(self, text: str):
        response = await self.client.audio.speech.create(
            model=self.config['tts_model'],
            voice=self.config['tts_voice'],
            input=text,
            response_format='opus'
        )
        temp_file = io.BytesIO()
        temp_file.write(response.read())
        temp_file.seek(0)
        return temp_file, len(text)

    async def embed_texts(self, texts: List[str], model: str | None = None) -> List[List[float]]:
        model = model or self.config.get("embedding_model", "text-embedding-3-small")
        resp = await self.client.embeddings.create(model=model, input=texts)
        return [item.embedding for item in resp.data]

    @retry(reraise=True, retry=retry_if_exception_type(openai.RateLimitError), wait=wait_fixed(20), stop=stop_after_attempt(3))
    async def _common_get_chat_response(self, chat_id: int, query: str, stream=False):
        if chat_id not in self.conversations:
            self.reset_chat_history(chat_id)

        self.last_updated[chat_id] = datetime.datetime.now()
        self._add_to_history(chat_id, "user", query)

        user_model = self.user_models.get(chat_id, self.config['model'])
        model_to_use = user_model if not self.conversations_vision[chat_id] else self.config['vision_model']
        common_args = {
            'model': model_to_use,
            'messages': self.conversations[chat_id],
            'temperature': self.config['temperature'],
            'n': self.config['n_choices'],
            'max_tokens': self.config['max_tokens'],
            'presence_penalty': self.config['presence_penalty'],
            'frequency_penalty': self.config['frequency_penalty'],
            'stream': stream
        }
        return await self.client.chat.completions.create(**common_args)

    def _add_to_history(self, chat_id: int, role: str, content: str):
        self.conversations[chat_id].append({"role": role, "content": content})

    def _count_tokens(self, messages) -> int:
        model = self.config['model']
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("o200k_base")
        tokens_per_message = 3
        tokens_per_name = 1
        num_tokens = 0
        for message in messages:
            num_tokens += tokens_per_message
            for key, value in message.items():
                if key == 'content':
                    if isinstance(value, str):
                        num_tokens += len(encoding.encode(value))
                else:
                    num_tokens += len(encoding.encode(value))
                    if key == "name":
                        num_tokens += tokens_per_name
        num_tokens += 3
        return num_tokens
