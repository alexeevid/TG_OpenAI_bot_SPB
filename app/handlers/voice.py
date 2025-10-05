from __future__ import annotations

import logging
import os
import tempfile
import inspect
from html import escape
from typing import Optional

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

# мягкий импорт settings
try:
    from app.core import settings
except Exception:
    try:
        from ..core import settings  # если структура пакетов как app/handlers/voice.py
    except Exception:
        class _S:  # дефолты на крайний случай
            SHOW_VOICE_TRANSCRIPT = True
            VOICE_TRANSCRIPT_MAXLEN = 400
        settings = _S()

from ..services.dialog_service import DialogService
from ..services.gen_service import GenService
from ..services.voice_service import VoiceService

log = logging.getLogger(__name__)


def _suffix_for_mime(mime: Optional[str]) -> str:
    if not mime:
        return ".ogg"
    m = mime.lower()
    if "ogg" in m: return ".ogg"
    if "mpeg" in m or "mp3" in m: return ".mp3"
    if "webm" in m: return ".webm"
    if "mp4" in m or "m4a" in m: return ".mp4"
    return ".ogg"


async def _transcribe_any(vs: VoiceService, *, tmp_path: str, message_obj) -> Optional[str]:
    try:
        tp = getattr(vs, "transcribe_path", None)
        if callable(tp):
            return (await tp(tmp_path)) if inspect.iscoroutinefunction(tp) else tp(tmp_path)
    except Exception as e:
        log.exception("VOICE: transcribe_path failed: %s", e)

    try:
        t = getattr(vs, "transcribe", None)
        if callable(t):
            return (await t(message_obj)) if inspect.iscoroutinefunction(t) else t(message_obj)
    except Exception as e:
        log.exception("VOICE: transcribe(message) failed: %s", e)

    return None


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.message
        if not msg:
            return

        voice = msg.voice
        audio = msg.audio
        if not voice and not audio:
            return

        fileish = voice or audio
        mime = getattr(fileish, "mime_type", None)
        duration = getattr(fileish, "duration", None)
        file_id = getattr(fileish, "file_id", None)
        log.info("VOICE: получен %s, duration=%s sec, mime=%s, file_id=%s",
                 "voice" if voice else "audio", duration, mime, file_id)

        fobj = await fileish.get_file()
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=_suffix_for_mime(mime)) as tmp:
                await fobj.download_to_drive(custom_path=tmp.name)
                tmp_path = tmp.name
            log.info("VOICE: файл скачан -> %s", tmp_path)

            vs: VoiceService = context.bot_data.get("svc_voice")
            if not vs:
                await msg.reply_text("⚠️ Голосовой сервис не сконфигурирован.")
                return

            text = await _transcribe_any(vs, tmp_path=tmp_path, message_obj=msg)

        finally:
            if tmp_path:
                try: os.remove(tmp_path)
                except Exception: pass

        if not text or not str(text).strip():
            await msg.reply_text("⚠️ Не удалось распознать голос. Попробуйте ещё раз.")
            log.warning("VOICE: empty transcript")
            return

        text = str(text).strip()
        log.info("VOICE: распознан текст: %r", text)

        if getattr(settings, "SHOW_VOICE_TRANSCRIPT", True):
            maxlen = int(getattr(settings, "VOICE_TRANSCRIPT_MAXLEN", 400))
            disp = text if len(text) <= maxlen else (text[:maxlen] + "…")
            await msg.reply_html(f"🗣️ <b>Распознал</b>: <i>{escape(disp)}</i>")

        ds: DialogService = context.bot_data.get("svc_dialog")
        gen: GenService = context.bot_data.get("svc_gen")
        if not ds or not gen:
            await msg.reply_text("⚠️ Сервисы диалогов/генерации не сконфигурированы.")
            log.error("VOICE: missing services: ds=%s, gen=%s", bool(ds), bool(gen))
            return

        d = ds.get_or_create_active(update.effective_user.id)
        ds.add_user_message(d.id, text)

        reply = ""
        try:
            ans = await gen.chat(user_msg=text, dialog_id=d.id)
            reply = (ans.text or "").strip() if ans else ""
        except Exception as e:
            log.exception("VOICE: gen.chat failed: %s", e)

        if not reply:
            reply = f"Распознал: {text}"

        ds.add_assistant_message(d.id, reply)
        await msg.reply_text(reply)

    except Exception as e:
        log.exception("VOICE handler crashed: %s", e)
        try:
            await update.message.reply_text(f"⚠️ Ошибка обработки голоса: {e.__class__.__name__}")
        except Exception:
            pass


def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
