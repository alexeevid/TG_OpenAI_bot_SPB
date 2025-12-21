from telegram import Update
from telegram.ext import ContextTypes
from ..services.authz_service import AuthzService
from ..services.dialog_service import DialogService

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return

    ds: DialogService = context.bot_data.get("svc_dialog")
    cfg = context.bot_data.get("settings")
    if not ds or not cfg or not update.effective_user:
        await update.message.reply_text("⚠️ Сервисы не настроены.")
        return

    d = ds.get_active_dialog(update.effective_user.id)
    settings = ds.get_active_settings(update.effective_user.id)

    # Информация по диалогу
    model = settings.get("text_model") or cfg.text_model
    mode = settings.get("mode") or "detailed"
    image_enabled = bool(context.bot_data.get("svc_image"))
    rag_enabled = bool(context.bot_data.get("svc_rag"))
    history = ds.history(d.id, limit=1000)
    total = len(history)
    user_count = sum(1 for m in history if getattr(m, "role", "") == "user")
    assistant_count = sum(1 for m in history if getattr(m, "role", "") == "assistant")

    text = (
        f"📄 Диалог: {d.id} — {d.title or '(без названия)'}\n"
        f"🤖 Модель: {model}  |  🎯 Режим: {mode}\n"
        f"💬 Сообщений: {total} (пользователь: {user_count}, ассистент: {assistant_count})\n"
        f"🖼️ Генерация изображений: {'включена' if image_enabled else 'отключена'}\n"
        f"📚 База знаний (RAG): {'включена' if rag_enabled else 'отключена'}"
    )
    await update.message.reply_text(text)

def register(app):
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("status", cmd_status))
