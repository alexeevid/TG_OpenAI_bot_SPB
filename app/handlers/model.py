from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from ..services.dialog_manager import get_current_dialog, update_dialog_settings

MODELS = ["gpt-4", "gpt-4o", "gpt-3.5-turbo"]

async def model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(model, callback_data=f"model|{model}")] for model in MODELS]
    await update.message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(keyboard))

async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, model = query.data.split("|")
    dialog = get_current_dialog(query.from_user.id)
    dialog.settings["model"] = model
    update_dialog_settings(dialog)
    await query.answer(f"Модель установлена: {model}")
    await query.edit_message_text(f"Модель установлена: {model}")
