import datetime, tiktoken
from openai import AsyncOpenAI
class OpenAIHelper:
    def __init__(self, config, plugin_manager):
        self.config=config; self.plugin_manager=plugin_manager
        self.client=AsyncOpenAI(api_key=config["api_key"])
        self.conversations={}; self.user_models={}; self.last_updated={}
    def reset_chat_history(self, chat_id, content=None):
        if not content: content=self.config.get("assistant_prompt","You are a helpful assistant.")
        self.conversations[chat_id]=[{"role":"system","content":content}]
    async def fetch_available_models(self):
        data = await self.client.models.list()
        return sorted([m.id for m in data.data])
    def allowed(self, lst):
        wl=self.config.get("allowed_models_whitelist"); dl=self.config.get("denylist_models")
        if wl: lst=[x for x in lst if x in {m.strip() for m in wl.split(',') if m.strip()}]
        if dl: lst=[x for x in lst if x not in {m.strip() for m in dl.split(',') if m.strip()}]
        return lst
    async def get_chat_response(self, chat_id, query):
        if chat_id not in self.conversations: self.reset_chat_history(chat_id)
        self.conversations[chat_id].append({"role":"user","content":query})
        model=self.user_models.get(chat_id,self.config["model"])
        resp = await self.client.chat.completions.create(
            model=model, messages=self.conversations[chat_id],
            temperature=self.config["temperature"], max_tokens=self.config["max_tokens"]
        )
        ans = resp.choices[0].message.content.strip()
        self.conversations[chat_id].append({"role":"assistant","content":ans})
        return ans, resp.usage
    async def interpret_image(self, chat_id, fileobj, prompt=None):
        import base64
        if prompt is None: prompt="Опиши, что на изображении."
        b64=base64.b64encode(fileobj.read()).decode("utf-8")
        content=[{"type":"text","text":prompt},{"type":"image_url","image_url":{"url":f"data:image/png;base64,{b64}"}}]
        resp=await self.client.chat.completions.create(model=self.config["vision_model"], messages=[{"role":"user","content":content}], max_tokens=self.config["vision_max_tokens"])
        ans=resp.choices[0].message.content.strip()
        self.conversations.setdefault(chat_id,[]).append({"role":"assistant","content":ans})
        return ans, resp.usage
    async def generate_image(self, prompt):
        resp = await self.client.images.generate(model=self.config["image_model"], prompt=prompt, n=1, size=self.config["image_size"])
        return resp.data[0].url, self.config["image_size"]
    async def transcribe(self, filename):
        with open(filename,"rb") as f:
            r=await self.client.audio.transcriptions.create(model="whisper-1", file=f)
            return r.text
