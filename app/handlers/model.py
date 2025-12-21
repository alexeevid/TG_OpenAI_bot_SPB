from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from ..services.dialog_service import DialogService
from ..services.gen_service import GenService
from ..services.authz_service import AuthzService

async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    ds: DialogService = context.bot_data.get("svc_dialog")
    gen: GenService = context.bot_data.get("svc_gen")
    if not ds or not gen or not update.effective_user:
        await update.message.reply_text("⚠️ Сервисы не настроены.")
        return
    # If a model name is provided as argument, set it immediately
    if context.args:
        model = context.args[0].strip()
        ds.update_active_settings(update.effective_user.id, {"text_model": model})
        await update.message.reply_text(f"Модель для текущего диалога установлена: {model}")
        return
    # Otherwise, show a selection of models
    models = await gen.selectable_models(limit=12)
    kb = [[InlineKeyboardButton(m, callback_data=f"model|{m}")] for m in models]
    await update.message.reply_text("Выберите модель для текущего диалога:", reply_markup=InlineKeyboardMarkup(kb))

async def on_model_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, model = (q.data or "").split("|", 1)
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and q.from_user and not az.is_allowed(q.from_user.id):
        await q.edit_message_text("⛔ Доступ запрещен.")
        return
    ds: DialogService = context.bot_data.get("svc_dialog")
    if not ds or not q.from_user:
        await q.edit_message_text("⚠️ Сервис диалогов не настроен.")
        return
    ds.update_active_settings(q.from_user.id, {"text_model": model})
    await q.edit_message_text(f"Модель для диалога установлена: {model}")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CallbackQueryHandler(on_model_cb, pattern=r"^model\|"))
