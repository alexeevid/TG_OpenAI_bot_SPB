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

            # читаем БАЙТЫ (это устраняет 'bytes-like object required, not str')
            raw = p.read_bytes()

            # 1) bytes-интерфейс
            fn_bytes = getattr(self._openai, "transcribe_bytes", None)
            if callable(fn_bytes):
                try:
                    if _is_coro_fn(fn_bytes):
                        text = await fn_bytes(raw, filename=p.name)
                    else:
                        text = fn_bytes(raw, filename=p.name)
                    text = (text or "").strip()
                    if text:
                        log.info("VOICE: распознано (bytes): %r", text)
                        return text
                except Exception as e:
                    log.exception("VOICE: transcribe_bytes failed: %s", e)

            # 2) file-like интерфейс
            fn_file = getattr(self._openai, "transcribe_file", None)
            if callable(fn_file):
                try:
                    with open(p, "rb") as f:
                        if _is_coro_fn(fn_file):
                            text = await fn_file(f)
                        else:
                            text = fn_file(f)
                    text = (text or "").strip()
                    if text:
                        log.info("VOICE: распознано (file): %r", text)
                        return text
                except Exception as e:
                    log.exception("VOICE: transcribe_file failed: %s", e)

            # 3) явный путь-строкой (если клиент так умеет)
            for name in ("transcribe_path", "transcribe"):
                fn = getattr(self._openai, name, None)
                if callable(fn):
                    try:
                        if _is_coro_fn(fn):
                            text = await fn(str(p))
                        else:
                            text = fn(str(p))   # ← передаём СТРОКУ пути, не Path
                        text = (text or "").strip()
                        if text:
                            log.info("VOICE: распознано (%s): %r", name, text)
                            return text
                    except Exception as e:
                        log.exception("VOICE: %s failed: %s", name, e)

            # fallback: ничего не распознали
            log.warning("VOICE: пустой результат распознавания для %s", p)
            return "[пустой результат распознавания]"

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
