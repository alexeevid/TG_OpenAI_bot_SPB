from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from ..services.dialog_service import DialogService
from ..services.authz_service import AuthzService

MODES = [
    ("concise", "Кратко"),
    ("detailed", "Подробно"),
    ("mcwilliams", "McWilliams-стиль"),
]

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    kb = [[InlineKeyboardButton(title, callback_data=f"mode|{key}")] for key, title in MODES]
    await update.message.reply_text("Выберите режим ответа для текущего диалога:", reply_markup=InlineKeyboardMarkup(kb))

async def on_mode_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, mode = (q.data or "").split("|", 1)
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and q.from_user and not az.is_allowed(q.from_user.id):
        await q.edit_message_text("⛔ Доступ запрещен.")
        return
    ds: DialogService = context.bot_data.get("svc_dialog")
    if not ds or not q.from_user:
        await q.edit_message_text("⚠️ Сервис диалогов не настроен.")
        return
    ds.update_active_settings(q.from_user.id, {"mode": mode})
    await q.edit_message_text(f"Режим для диалога установлен: {mode}")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CallbackQueryHandler(on_mode_cb, pattern=r"^mode\|"))
