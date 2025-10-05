import logging
from pathlib import Path

log = logging.getLogger(__name__)

class VoiceService:
    """
    Совместим с bootstrap: __init__(openai_client, settings)
    Безопасно открывает аудио как bytes и передаёт клиенту.
    """

    def __init__(self, openai_client, settings=None):
        self._openai = openai_client
        self._settings = settings

    async def transcribe_path(self, path: str | Path) -> str:
        try:
            p = Path(path)
            if not p.exists():
                log.error("VOICE: файл не найден: %s", p)
                return "[ошибка: файл не найден]"

            text = None
            # если у клиента есть метод, принимающий file-объект
            fn_file = getattr(self._openai, "transcribe_file", None)
            if callable(fn_file):
                try:
                    with open(p, "rb") as f:
                        text = fn_file(f)
                except Exception as e:
                    log.exception("VOICE: transcribe_file(f) failed: %s", e)

            # fallback — если есть метод, принимающий путь
            if not text:
                fn_path = getattr(self._openai, "transcribe", None)
                if callable(fn_path):
                    text = fn_path(str(p))  # передаём СТРОКОВЫЙ путь

            text = (text or "").strip()
            if not text:
                text = "[пустой результат распознавания]"
            log.info("VOICE: распознан текст: %r", text)
            return text

        except Exception as e:
            log.exception("VOICE: ошибка транскрипции: %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"

    async def transcribe(self, message) -> str:
        """Совместимость со старым интерфейсом: принимает Telegram message (voice)."""
        try:
            file = await message.voice.get_file()
            local_path = f"/tmp/{file.file_unique_id}.ogg"
            await file.download_to_drive(custom_path=local_path)
            return await self.transcribe_path(local_path)
        except Exception as e:
            log.exception("VOICE: ошибка в transcribe(): %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"
