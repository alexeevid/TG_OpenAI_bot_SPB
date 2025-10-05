
from ..clients.openai_client import OpenAIClient
from ..core.types import Transcript
import logging
log = logging.getLogger(__name__)

class VoiceService:
    def __init__(self, openai: OpenAIClient, settings):
        self._openai = openai
        self._settings = settings
    async def transcribe(self, audio_bytes: bytes, lang_hint: str|None=None) -> Transcript:
        text = self._openai.transcribe(audio_bytes, model=self._settings.transcribe_model)
        return Transcript(text=text or "[empty]", lang=lang_hint, duration_sec=None)
    
    async def transcribe(self, message):
        try:
            file = await message.voice.get_file()
            path = await file.download_to_drive()
            log.info("VOICE: скачан файл %s", path)
            text = self._openai.transcribe(path)
            log.info("VOICE: распознан текст = %s", text)
            return text
        except Exception as e:
            log.exception("VOICE: ошибка транскрипции %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"
