from __future__ import annotations
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

from ..services.dialog_service import DialogService
from ..services.gen_service import GenService

log = logging.getLogger(__name__)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.message
        if not msg or not msg.text:
            return

        ds: DialogService = context.bot_data.get("svc_dialog")
        gen: GenService = context.bot_data.get("svc_gen")
        if not ds or not gen:
            await msg.reply_text("⚠️ Сервисы диалогов/генерации не сконфигурированы.")
            return

        d = ds.get_or_create_active(update.effective_user.id)
        # тянем историю последних N реплик (минимум)
        history = ds.get_last_history_as_messages(d.id, limit=8)  # верни [{'role','content'}, ...]

        # настройки диалога (модель, стиль)
        st = ds.get_settings(d.id) or {}
        model = st.get("model")
        style = st.get("style")

        user_text = msg.text.strip()
        ds.add_user_message(d.id, user_text)

        ans = await gen.chat(
            user_msg=user_text,
            dialog_id=d.id,
            history=history,
            model=model,
            style=style,
        )
        reply = (ans.text or "").strip()
        ds.add_assistant_message(d.id, reply)
        await msg.reply_text(reply)

    except Exception as e:
        log.exception("TEXT handler error: %s", e)
        try:
            await update.message.reply_text("⚠️ Ошибка обработки текста.")
        except Exception:
            pass

def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
