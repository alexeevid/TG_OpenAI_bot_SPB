import logging
from pathlib import Path

log = logging.getLogger(__name__)

class VoiceService:
    """
    Универсальный сервис распознавания:
    - Совместим с текущим bootstrap: __init__(openai_client, settings)
    - Три безопасных слоя:
        1) transcribe_path(path): открывает файл в 'rb' и вызывает клиент
        2) transcribe(message): скачивает voice и проксирует в transcribe_path
        3) Любые исключения -> лог + человекочитаемое сообщение
    Ожидается, что openai_client имеет метод transcribe(file_or_path),
    как в нашем OpenAIClient-обёртке.
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

            # Пытаемся сначала через бинарный поток (надёжно для Whisper)
            text = None
            try:
                # Если у клиента есть low-level метод, принимающий file-объект:
                fn = getattr(self._openai, "transcribe_file", None)
                if callable(fn):
                    with open(p, "rb") as f:
                        text = fn(f)
            except Exception as e:
                log.exception("VOICE: transcribe_file(f) failed: %s", e)

            # Фолбэк: если есть high-level метод, принимающий путь
            if not text:
                fn2 = getattr(self._openai, "transcribe", None)
                if callable(fn2):
                    text = fn2(str(p))  # <-- передаём строковый путь, не Path

            # Защита от пустого результата
            text = (text or "").strip()
            if not text:
                text = "[пустой результат распознавания]"
            log.info("VOICE: распознан текст: %r", text)
            return text

        except Exception as e:
            log.exception("VOICE: ошибка транскрипции: %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"

    async def transcribe(self, message) -> str:
        """Совместимость со старым интерфейсом: принимает Telegram message."""
        try:
            file = await message.voice.get_file()
            # Сохраняем под стабильным именем (voice note присылается как OGG/WEBM)
            local_path = f"/tmp/{file.file_unique_id}.ogg"
            await file.download_to_drive(custom_path=local_path)
            return await self.transcribe_path(local_path)
        except Exception as e:
            log.exception("VOICE: ошибка в transcribe(): %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"
