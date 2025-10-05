import logging
from pathlib import Path
import asyncio

log = logging.getLogger(__name__)

def _is_coro_fn(fn):
    try:
        return asyncio.iscoroutinefunction(fn)
    except Exception:
        return False

class VoiceService:
    """
    Совместим с bootstrap: __init__(openai_client, settings).
    Нормализует аудио в байты и пробует интерфейсы клиента в порядке:
    1) transcribe_bytes(raw_bytes, filename="audio.ogg")
    2) transcribe_file(file_obj)          # file-like (rb)
    3) transcribe_path(str_path)          # строковый путь
    4) transcribe(str_path)               # старый путь
    Любая ошибка → человекочитаемое сообщение, без падения процесса.
    """

    def __init__(self, openai_client, settings=None):
        self._openai = openai_client
        self._settings = settings

    async def transcribe_path(self, path: str | Path) -> str:
        p = Path(path)
        try:
            if not p.exists():
                log.error("VOICE: файл не найден: %s", p)
                return "[ошибка: файл не найден]"

            # 🔹 исправление: открываем файл как bytes
            with open(p, "rb") as f:
                audio_bytes = f.read()

            # Если клиент умеет transcribe_bytes — используем его
            if hasattr(self._openai, "transcribe_bytes"):
                text = self._openai.transcribe_bytes(audio_bytes, filename=p.name)

            # Если клиент умеет transcribe_file — используем file-like
            elif hasattr(self._openai, "transcribe_file"):
                with open(p, "rb") as f:
                    text = self._openai.transcribe_file(f)

            # Если только общий метод transcribe, но он ожидает bytes
            elif hasattr(self._openai, "transcribe"):
                with open(p, "rb") as f:
                    text = self._openai.transcribe(f)

            else:
                log.error("VOICE: метод транскрипции не найден в OpenAIClient")
                return "[ошибка: не найден метод транскрипции]"

            text = (text or "").strip()
            if not text:
                text = "[пустой результат распознавания]"

            log.info("VOICE: успешно распознан текст: %s", text)
            return text

        except Exception as e:
            log.exception("VOICE: ошибка транскрипции: %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"

    async def transcribe(self, message) -> str:
        """Совместимость: принимает Telegram voice message."""
        try:
            file = await message.voice.get_file()
            local_path = f"/tmp/{file.file_unique_id}.ogg"
            await file.download_to_drive(custom_path=local_path)
            return await self.transcribe_path(local_path)
        except Exception as e:
            log.exception("VOICE: ошибка в transcribe(): %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"
