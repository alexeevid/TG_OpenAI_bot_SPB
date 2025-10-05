from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import logging
from ..services.gen_service import GenService
from ..services.dialog_service import DialogService

log = logging.getLogger(__name__)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message or not update.message.text:
            return

        ds: DialogService = context.bot_data["svc_dialog"]
        gen: GenService = context.bot_data["svc_gen"]

        # Берём последний диалог (создаём только если ни одного нет)
        d = ds.get_or_create_active(update.effective_user.id)

        question = update.message.text.strip()
        if not question:
            await update.message.reply_text("⚠️ Пустое сообщение.")
            return

        ds.add_user_message(d.id, question)
        log.info("TEXT: user=%s dialog=%s msg_len=%s", update.effective_user.id, d.id, len(question))

        # Основная генерация с защитой
        try:
            ans = await gen.chat(user_msg=question, dialog_id=d.id)
            text = (ans.text or "").strip() if ans else ""
        except Exception as e:
            log.exception("TEXT: gen.chat failed: %s", e)
            await update.message.reply_text(f"⚠️ Ошибка генерации: {e.__class__.__name__}")
            return

        if not text:
            # Явный фолбэк, чтобы в чате не было тишины
            text = "Я получил ваше сообщение, но не смог сгенерировать ответ. Попробуйте переформулировать или /reset."
        ds.add_assistant_message(d.id, text)
        await update.message.reply_text(text)

    except Exception as e:
        log.exception("TEXT handler crashed: %s", e)
        try:
            await update.message.reply_text(f"⚠️ Ошибка обработки: {e.__class__.__name__}")
        except Exception:
            pass

def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
