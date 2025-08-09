from openai import AsyncOpenAI
from bot.settings import load_settings
_settings = load_settings()
_client = AsyncOpenAI(api_key=_settings.openai_api_key)
async def chat(messages, model=None, max_tokens=800):
    model = model or _settings.openai_model
    resp = await _client.chat.completions.create(model=model, messages=messages, temperature=0.2, max_tokens=800)
    return resp.choices[0].message.content or ''
async def embed(texts, model=None):
    model = model or _settings.embedding_model
    resp = await _client.embeddings.create(model=model, input=texts)
    return [d.embedding for d in resp.data]
async def transcribe_audio(file_path: str) -> str:
    with open(file_path, 'rb') as f:
        tr = await _client.audio.transcriptions.create(model='whisper-1', file=f)
    return tr.text or ''
async def generate_image(prompt: str) -> bytes:
    img = await _client.images.generate(model=_settings.image_model, prompt=prompt, size='1024x1024')
    import requests
    r = requests.get(img.data[0].url, timeout=60); r.raise_for_status(); return r.content
