from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class VoiceService:
    """Распознавание голоса через OpenAI (whisper-1 по умолчанию).

    openai_client должен предоставлять метод transcribe_file(fobj, model=...).
    """

    def __init__(self, openai_client, settings=None):
        self._openai = openai_client
        self._settings = settings

    async def transcribe_path(self, path: str | Path) -> str:
        p = Path(path)
        if not p.exists():
            return "[ошибка распознавания: файл не найден]"

        model = None
        if self._settings is not None:
            model = getattr(self._settings, "openai_transcribe_model", None) or getattr(self._settings, "transcribe_model", None)

        try:
            with p.open("rb") as f:
                if model:
                    return await asyncio.to_thread(self._openai.transcribe_file, f, model)
                return await asyncio.to_thread(self._openai.transcribe_file, f)
        except Exception as e:
            log.exception("VOICE: transcribe_path failed: %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"

    async def transcribe(self, message) -> str:
        """Скачивает voice/audio в /tmp и транскрибирует."""
        try:
            tg_file = await message.voice.get_file() if message.voice else await message.audio.get_file()
            local_path = f"/tmp/{tg_file.file_unique_id}.ogg"
            await tg_file.download_to_drive(custom_path=local_path)
            return await self.transcribe_path(local_path)
        except Exception as e:
            log.exception("VOICE: transcribe failed: %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"
