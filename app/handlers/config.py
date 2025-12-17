from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


async def config_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.application.bot_data.get("settings")
    if cfg:
        reply = f"Текущая модель: {cfg.text_model}\nИзображения: {'Да' if cfg.enable_image_generation else 'Нет'}"
    else:
        reply = "Настройки не найдены."

    await update.message.reply_text(reply)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("config", config_handler))
