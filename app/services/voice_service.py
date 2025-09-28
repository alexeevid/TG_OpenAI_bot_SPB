
from ..clients.openai_client import OpenAIClient
from ..core.types import Transcript

class VoiceService:
    def __init__(self, openai: OpenAIClient, settings):
        self._openai = openai
        self._settings = settings
    async def transcribe(self, audio_bytes: bytes, lang_hint: str|None=None) -> Transcript:
        text = self._openai.transcribe(audio_bytes, model=self._settings.transcribe_model)
        return Transcript(text=text or "[empty]", lang=lang_hint, duration_sec=None)
