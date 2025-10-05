from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import logging, tempfile, os

from ..services.dialog_service import DialogService
from ..services.gen_service import GenService
from ..services.voice_service import VoiceService

log = logging.getLogger(__name__)

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Страховка на "пустые" апдейты
        if not update.message or not update.message.voice:
            return

        voice = update.message.voice
        log.info("VOICE: получен voice, duration=%s sec, mime=%s, file_id=%s",
                 getattr(voice, "duration", None), getattr(voice, "mime_type", None), getattr(voice, "file_id", None))

        # Скачиваем файл в tmp (OGG/WEBM)
        f = await voice.get_file()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            await f.download_to_drive(custom_path=tmp.name)
            tmp_path = tmp.name
        log.info("VOICE: файл скачан -> %s", tmp_path)

        # Распознание
        vs: VoiceService = context.bot_data["svc_voice"]
        text = None

        # Универсальный вызов: пробуем разные сигнатуры (на случай другой реализации сервиса)
        try:
            # если в сервисе есть метод по пути файла (лучший вариант)
            transcribe_path = getattr(vs, "transcribe_path", None)
            if callable(transcribe_path):
                text = await transcribe_path(tmp_path) if transcribe_path.__code__.co_flags & 0x80 else transcribe_path(tmp_path)
        except Exception as e:
            log.exception("VOICE: transcribe_path failed: %s", e)

        if not text:
            try:
                # старый вариант: метод принимает message
                transcribe = getattr(vs, "transcribe", None)
                if callable(transcribe):
                    text = await transcribe(update.message) if transcribe.__code__.co_flags & 0x80 else transcribe(update.message)
            except Exception as e:
                log.exception("VOICE: transcribe(message) failed: %s", e)

        # Чистим tmp
        try:
            os.remove(tmp_path)
        except Exception:
            pass

        if not text:
            await update.message.reply_text("⚠️ Не удалось распознать голос. Попробуйте ещё раз.")
            return

        log.info("VOICE: распознан текст: %r", text)

        # Склеиваем с диалогом и отвечаем моделью (как on_text)
        ds: DialogService = context.bot_data["svc_dialog"]
        gen: GenService = context.bot_data["svc_gen"]

        d = ds.get_or_create_active(update.effective_user.id)
        ds.add_user_message(d.id, text)

        try:
            ans = await gen.chat(user_msg=text, dialog_id=d.id)
            reply = (ans.text or "").strip() if ans else ""
        except Exception as e:
            log.exception("VOICE: gen.chat failed: %s", e)
            reply = ""

        if not reply:
            reply = f"Распознал: {text}"

        ds.add_assistant_message(d.id, reply)
        await update.message.reply_text(reply)

    except Exception as e:
        log.exception("VOICE handler crashed: %s", e)
        try:
            await update.message.reply_text(f"⚠️ Ошибка обработки голоса: {e.__class__.__name__}")
        except Exception:
            pass

def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))

