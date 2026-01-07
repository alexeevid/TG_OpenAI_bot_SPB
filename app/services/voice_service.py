from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


class VoiceService:
    """
    Распознавание голоса через OpenAI.

    В версии проекта (25) OpenAIClient предоставляет метод:
        transcribe(audio_bytes: bytes, *, model: str) -> str

    Поэтому VoiceService читает файл в bytes и вызывает openai_client.transcribe(...).
    """

    def __init__(self, openai_client, settings=None):
        self._openai = openai_client
        self._settings = settings

    def _default_model(self) -> str:
        # Пытаемся взять из settings, иначе — whisper-1
        if self._settings is not None:
            m = (
                getattr(self._settings, "openai_transcribe_model", None)
                or getattr(self._settings, "transcribe_model", None)
            )
            if m:
                return str(m)
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
            return ""

        # приоритет: model аргумент > dialog_settings > settings default
        desired_model = (
            model
            or self._pick_from_dialog_settings(dialog_settings, "transcribe_model")
            or self._default_model()
        )

        try:
            audio_bytes = p.read_bytes()

            # OpenAIClient.transcribe синхронный -> в thread
            text = await asyncio.to_thread(self._openai.transcribe, audio_bytes, model=desired_model)
            return (text or "").strip()
        except Exception as e:
            log.exception("VOICE: transcribe_path failed: %s", e)
            # Возвращаем пусто, чтобы handler показал нормальную ошибку UX-уровня
            return ""

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
            return ""
