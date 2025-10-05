import logging
import os
from pathlib import Path
from openai import OpenAI

log = logging.getLogger(__name__)

class VoiceService:
    def __init__(self, openai_client: OpenAI):
        self._openai = openai_client

    async def transcribe_path(self, path: str | Path) -> str:
        """Распознать речь из аудиофайла через Whisper (OpenAI)."""
        try:
            p = Path(path)
            if not p.exists():
                log.error("VOICE: файл не найден: %s", path)
                return "[ошибка: файл не найден]"

            # OpenAI API ожидает байтовый поток или открытый файл
            with open(p, "rb") as f:
                result = self._openai.audio.transcriptions.create(
                    model="whisper-1",
                    file=f
                )
            text = (result.text or "").strip()
            if not text:
                text = "[пустой результат распознавания]"
            log.info("VOICE: Whisper распознал: %s", text)
            return text

        except Exception as e:
            log.exception("VOICE: ошибка транскрипции: %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"

    async def transcribe(self, message) -> str:
        """Совместимость со старым интерфейсом (принимает Telegram message)."""
        try:
            file = await message.voice.get_file()
            local_path = f"/tmp/{file.file_unique_id}.ogg"
            await file.download_to_drive(custom_path=local_path)
            return await self.transcribe_path(local_path)
        except Exception as e:
            log.exception("VOICE: ошибка в transcribe(): %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"
