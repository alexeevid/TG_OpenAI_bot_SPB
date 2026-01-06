from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


class VoiceService:
    """Распознавание голоса через OpenAI (whisper-1 по умолчанию).

    openai_client должен предоставлять метод transcribe_file(fobj, model=...).
    """

    def __init__(self, openai_client, settings=None):
        self._openai = openai_client
        self._settings = settings

    def _default_model(self) -> str:
        if self._settings is not None:
            return (
                getattr(self._settings, "openai_transcribe_model", None)
                or getattr(self._settings, "transcribe_model", None)
                or "whisper-1"
            )
        return "whisper-1"

    def _pick_from_dialog_settings(self, dialog_settings: Optional[Dict[str, Any]], key: str) -> Optional[str]:
        if dialog_settings and isinstance(dialog_settings, dict):
            v = dialog_settings.get(key)
            if v:
                return str(v)
        return None

    async def transcribe_path(
        self,
        path: str | Path,
        *,
        model: Optional[str] = None,
        dialog_settings: Optional[Dict[str, Any]] = None,
    ) -> str:
        p = Path(path)
        if not p.exists():
            return "[ошибка распознавания: файл не найден]"

        # приоритет: model аргумент > dialog_settings > settings default
        desired_model = model or self._pick_from_dialog_settings(dialog_settings, "transcribe_model") or self._default_model()

        try:
            with p.open("rb") as f:
                # OpenAIClient.transcribe_file умеет принимать model как именованный аргумент,
                # но в нашей обёртке допускается и позиционный.
                try:
                    return await asyncio.to_thread(self._openai.transcribe_file, f, desired_model)
                except TypeError:
                    return await asyncio.to_thread(self._openai.transcribe_file, f, model=desired_model)
        except Exception as e:
            log.exception("VOICE: transcribe_path failed: %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"

    async def transcribe(
        self,
        message,
        *,
        model: Optional[str] = None,
        dialog_settings: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Скачивает voice/audio в /tmp и транскрибирует."""
        try:
            tg_file = await message.voice.get_file() if message.voice else await message.audio.get_file()
            local_path = f"/tmp/{tg_file.file_unique_id}.ogg"
            await tg_file.download_to_drive(custom_path=local_path)
            return await self.transcribe_path(local_path, model=model, dialog_settings=dialog_settings)
        except Exception as e:
            log.exception("VOICE: transcribe failed: %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"
