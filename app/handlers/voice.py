from __future__ import annotations

import logging
import os
import tempfile
import inspect
from html import escape
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, ContextTypes, filters

from ..services.dialog_service import DialogService
from ..services.gen_service import GenService
from ..services.voice_service import VoiceService
from app.core import settings

log = logging.getLogger(__name__)


def _suffix_for_mime(mime: Optional[str]) -> str:
    """
    Подбираем безопасный суффикс временного файла для аудио.
    """
    if not mime:
        return ".ogg"
    mime = mime.lower()
    if "ogg" in mime:
        return ".ogg"
    if "mpeg" in mime or "mp3" in mime:
        return ".mp3"
    if "webm" in mime:
        return ".webm"
    if "mp4" in mime or "m4a" in mime:
        return ".mp4"
    return ".ogg"


async def _transcribe_any(
    vs: VoiceService,
    *,
    tmp_path: str,
    message_obj
) -> Optional[str]:
    """
    Универсальный вызов транскрибации:
    1) пробуем vs.transcribe_path(path) (async/sync),
    2) затем vs.transcribe(message) (async/sync),
    3) при ошибках — логируем и даём шанс следующему способу.
    """
    # 1) transcribe_path
    try:
        transcribe_path = getattr(vs, "transcribe_path", None)
        if callable(transcribe_path):
            if inspect.iscoroutinefunction(transcribe_path):
                return await transcribe_path(tmp_path)
            else:
                return transcribe_path(tmp_path)
    except Exception as e:
        log.exception("VOICE: transcribe_path failed: %s", e)

    # 2) transcribe(message)
    try:
        transcribe = getattr(vs, "transcribe", None)
        if callable(transcribe):
            if inspect.iscoroutinefunction(transcribe):
                return await transcribe(message_obj)
            else:
                return transcribe(message_obj)
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
            # Страховка: сюда попадаем только на VOICE|AUDIO
            return

        fileish = voice or audio
        mime = getattr(fileish, "mime_type", None)
        duration = getattr(fileish, "duration", None)
        file_id = getattr(fileish, "file_id", None)

        log.info(
            "VOICE: получен %s, duration=%s sec, mime=%s, file_id=%s",
            "voice" if voice else "audio", duration, mime, file_id
        )

        # Скачиваем аудио
        fobj = await fileish.get_file()
        tmp_path = None
        try:
            suffix = _suffix_for_mime(mime)
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                await fobj.download_to_drive(custom_path=tmp.name)
                tmp_path = tmp.name
            log.info("VOICE: файл скачан -> %s", tmp_path)

            # Транскрибация
            vs: VoiceService = context.bot_data.get("svc_voice")
            if not vs:
                await msg.reply_text("⚠️ Голосовой сервис не сконфигурирован.")
                return

            try:
                text = await _transcribe_any(vs, tmp_path=tmp_path, message_obj=msg)
            except Exception as e:
                log.exception("VOICE: transcription crashed: %s", e)
                await msg.reply_text("⚠️ Не удалось распознать голос. Попробуйте ещё раз.")
                return

        finally:
            # Чистим tmp (если успели создать)
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        if not text or not str(text).strip():
            await msg.reply_text("⚠️ Не удалось распознать голос. Попробуйте ещё раз.")
            log.warning("VOICE: empty transcript")
            return

        text = str(text).strip()
        log.info("VOICE: распознан текст: %r", text)

        # Показать расшифровку (если включено)
        if getattr(settings, "SHOW_VOICE_TRANSCRIPT", True):
            maxlen = int(getattr(settings, "VOICE_TRANSCRIPT_MAXLEN", 400))
            disp = text if len(text) <= maxlen else (text[:maxlen] + "…")
            try:
                await msg.chat.send_action(ChatAction.TYPING)
            except Exception:
                pass
            await msg.reply_html(f"🗣️ <b>Распознал</b>: <i>{escape(disp)}</i>")

        # Диалог и генерация ответа (как on_text)
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
            # Ожидаем, что gen.chat — async и возвращает объект с полем .text
            ans = await gen.chat(user_msg=text, dialog_id=d.id)
            reply = (ans.text or "").strip() if ans else ""
        except Exception as e:
            log.exception("VOICE: gen.chat failed: %s", e)

        if not reply:
            # Фоллбек — хотя бы отразить распознанный текст
            reply = f"Распознал: {text}"

        ds.add_assistant_message(d.id, reply)

        try:
            await msg.chat.send_action(ChatAction.TYPING)
        except Exception:
            pass

        await msg.reply_text(reply)

    except Exception as e:
        log.exception("VOICE handler crashed: %s", e)
        try:
            await update.message.reply_text(f"⚠️ Ошибка обработки голоса: {e.__class__.__name__}")
        except Exception:
            pass


def register(app: Application) -> None:
    """
    Регистрация хендлера. Охватываем voice и обычные аудио (mp3/m4a и т.д.).
    """
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
