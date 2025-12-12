from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from ..services.dialog_manager import get_current_dialog, update_dialog_settings

STYLES = ["default", "concise", "mcwilliams"]

async def style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(style, callback_data=f"style|{style}")] for style in STYLES]
    await update.message.reply_text("Выберите стиль:", reply_markup=InlineKeyboardMarkup(keyboard))

async def style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, style = query.data.split("|")
    dialog = get_current_dialog(query.from_user.id)
    dialog.settings["style"] = style
    update_dialog_settings(dialog)
    await query.answer(f"Стиль установлен: {style}")
    await query.edit_message_text(f"Стиль установлен: {style}")
