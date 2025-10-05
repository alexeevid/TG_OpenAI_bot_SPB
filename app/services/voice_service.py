import logging
from pathlib import Path
import asyncio

log = logging.getLogger(__name__)

class VoiceService:
    def __init__(self, openai_client, settings=None):
        self._openai = openai_client
        self._settings = settings

    async def _run_io(self, fn, *args, **kwargs):
        # Выполняем синхронный вызов SDK в threadpool, чтобы не блокировать event-loop
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def transcribe_path(self, path: str | Path) -> str:
        p = Path(path)
        try:
            if not p.exists():
                log.error("VOICE: файл не найден: %s", p)
                return "[ошибка распознавания: file_not_found]"

            raw = p.read_bytes()

            # 1) предпочтительно — через bytes → BytesIO
            if hasattr(self._openai, "transcribe_bytes"):
                try:
                    text = await self._run_io(self._openai.transcribe_bytes, raw, p.name)
                    text = (text or "").strip()
                    if text:
                        log.info("VOICE: распознан (bytes): %s", text)
                        return text
                except Exception as e:
                    log.exception("VOICE: transcribe_bytes failed: %s", e)

            # 2) через file-like
            if hasattr(self._openai, "transcribe_file"):
                try:
                    with open(p, "rb") as f:
                        text = await self._run_io(self._openai.transcribe_file, f)
                    text = (text or "").strip()
                    if text:
                        log.info("VOICE: распознан (file): %s", text)
                        return text
                except Exception as e:
                    log.exception("VOICE: transcribe_file failed: %s", e)

            # 3) через path (мы всё равно откроем файл внутри клиента)
            if hasattr(self._openai, "transcribe_path"):
                try:
                    text = await self._run_io(self._openai.transcribe_path, str(p))
                    text = (text or "").strip()
                    if text:
                        log.info("VOICE: распознан (path): %s", text)
                        return text
                except Exception as e:
                    log.exception("VOICE: transcribe_path failed: %s", e)

            log.warning("VOICE: пустой результат распознавания: %s", p)
            return "[ошибка распознавания: empty]"

        except Exception as e:
            log.exception("VOICE: ошибка транскрипции: %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"

    async def transcribe(self, message) -> str:
        try:
            file = await message.voice.get_file()
            local_path = f"/tmp/{file.file_unique_id}.ogg"
            await file.download_to_drive(custom_path=local_path)
            return await self.transcribe_path(local_path)
        except Exception as e:
            log.exception("VOICE: ошибка в transcribe(): %s", e)
            return f"[ошибка распознавания: {e.__class__.__name__}]"
