import logging
from telegram import Update
from telegram.ext import Application, ContextTypes

log = logging.getLogger(__name__)

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Логируем всё
    log.exception("UNHANDLED ERROR: %s", context.error)
    # Пытаемся уведомить пользователя
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Внутренняя ошибка обработчика. Сообщение записано в логи.")
    except Exception:
        pass

def register(app: Application) -> None:
    app.add_error_handler(on_error)
