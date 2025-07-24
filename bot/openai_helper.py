from __future__ import annotations

import datetime
from typing import Dict, List
import tiktoken
import openai
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

class OpenAIHelper:
    def __init__(self, config: dict, plugin_manager):
        self.config = config
        self.plugin_manager = plugin_manager
        self.client = AsyncOpenAI(api_key=config["api_key"])
        self.conversations: Dict[int, List[dict]] = {}
        self.conversations_vision: Dict[int, bool] = {}
        self.last_updated: Dict[int, datetime.datetime] = {}
        self.user_models: Dict[int, str] = {}
        self._models_cache = []
        self._models_cache_ts = None

    async def fetch_available_models(self, cache_minutes: int = 30) -> list[str]:
        now = datetime.datetime.utcnow()
        if self._models_cache_ts and (now - self._models_cache_ts).total_seconds() < cache_minutes * 60:
            return self._models_cache
        data = await self.client.models.list()
        names = [m.id for m in data.data]
        self._models_cache = names
        self._models_cache_ts = now
        return names

    def allowed_models(self, fetched: list[str]) -> list[str]:
        wl = self.config.get("allowed_models_whitelist")
        dl = self.config.get("denylist_models")
        if wl:
            s = set(x.strip() for x in wl.split(",") if x.strip())
            fetched = [m for m in fetched if m in s]
        if dl:
            s = set(x.strip() for x in dl.split(",") if x.strip())
            fetched = [m for m in fetched if m not in s]
        return sorted(fetched)

    def reset_chat_history(self, chat_id: int, content: str | None = None):
        if not content:
            content = self.config.get("assistant_prompt", "You are a helpful assistant.")
        self.conversations[chat_id] = [{"role":"system","content":content}]
        self.conversations_vision[chat_id] = False

    def __add_to_history(self, chat_id, role, content):
        self.conversations[chat_id].append({"role":role,"content":content})

    def __max_age_reached(self, chat_id) -> bool:
        if chat_id not in self.last_updated:
            return False
        last = self.last_updated[chat_id]
        now = datetime.datetime.now()
        return last < now - datetime.timedelta(minutes=self.config.get("max_conversation_age_minutes",60))

    def __count_tokens(self, messages) -> int:
        model = self.config["model"]
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("o200k_base")
        tokens = 0
        for m in messages:
            tokens += 3
            content = m.get("content","")
            if isinstance(content,str):
                tokens += len(enc.encode(content))
            else:
                for part in content:
                    if part["type"]=="text":
                        tokens += len(enc.encode(part["text"]))
                    else:
                        tokens += 200
        return tokens + 3

    async def __summarise(self, convo) -> str:
        resp = await self.client.chat.completions.create(
            model=self.config["model"],
            messages=[{"role":"system","content":"Summarize this conversation in 700 characters or less"},
                      {"role":"user","content":str(convo)}],
            temperature=0.4
        )
        return resp.choices[0].message.content

    async def get_chat_response(self, chat_id: int, query: str):
        res = await self.__common_get_chat_response(chat_id, query)
        text = res.choices[0].message.content.strip()
        self.__add_to_history(chat_id,"assistant",text)
        return text, res.usage

    @retry(reraise=True, retry=retry_if_exception_type(openai.RateLimitError), wait=wait_fixed(20), stop=stop_after_attempt(3))
    async def __common_get_chat_response(self, chat_id:int, query:str, stream:bool=False):
        if chat_id not in self.conversations or self.__max_age_reached(chat_id):
            self.reset_chat_history(chat_id)
        self.last_updated[chat_id] = datetime.datetime.now()
        self.__add_to_history(chat_id,"user",query)

        token_count = self.__count_tokens(self.conversations[chat_id])
        if token_count + self.config["max_tokens"] > 100000 or len(self.conversations[chat_id]) > self.config["max_history_size"]:
            try:
                summary = await self.__summarise(self.conversations[chat_id][:-1])
                self.reset_chat_history(chat_id, self.conversations[chat_id][0]["content"])
                self.__add_to_history(chat_id, "assistant", summary)
                self.__add_to_history(chat_id, "user", query)
            except Exception:
                self.conversations[chat_id] = self.conversations[chat_id][-self.config["max_history_size"]:]

        model = self.user_models.get(chat_id, self.config["model"])
        args = {
            "model": model,
            "messages": self.conversations[chat_id],
            "temperature": self.config["temperature"],
            "max_tokens": self.config["max_tokens"],
            "n": 1,
            "stream": stream
        }
        return await self.client.chat.completions.create(**args)

    async def interpret_image(self, chat_id: int, fileobj, prompt: str | None = None):
        import base64
        if prompt is None:
            prompt = self.config.get("vision_prompt","Опиши, что на изображении.")
        b64 = base64.b64encode(fileobj.read()).decode("utf-8")
        content = [
            {"type":"text","text":prompt},
            {"type":"image_url","image_url":{"url":f"data:image/png;base64,{b64}"}}
        ]
        resp = await self.client.chat.completions.create(
            model=self.config["vision_model"],
            messages=[{"role":"user","content":content}],
            max_tokens=self.config["vision_max_tokens"],
            temperature=self.config["temperature"]
        )
        text = resp.choices[0].message.content.strip()
        self.__add_to_history(chat_id,"assistant",text)
        return text, resp.usage

    async def generate_image(self, prompt: str):
        resp = await self.client.images.generate(
            model=self.config["image_model"],
            prompt=prompt,
            size=self.config["image_size"],
            n=1
        )
        return resp.data[0].url, self.config["image_size"]

    async def transcribe(self, filename: str) -> str:
        with open(filename,"rb") as f:
            result = await self.client.audio.transcriptions.create(
                model="whisper-1", file=f, prompt=self.config.get("whisper_prompt","")
            )
            return result.text
