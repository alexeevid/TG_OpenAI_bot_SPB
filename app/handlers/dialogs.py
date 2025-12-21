from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.dialog_service import DialogService
from ..services.authz_service import AuthzService

async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    ds: DialogService = context.bot_data.get("svc_dialog")
    if not ds or not update.effective_user:
        await update.message.reply_text("⚠️ Сервис диалогов не настроен.")
        return
    dialogs = ds.list_dialogs(update.effective_user.id, limit=20)
    active = ds.get_active_dialog(update.effective_user.id)
    if not dialogs:
        await update.message.reply_text("Диалогов пока нет. Используйте /reset для создания.")
        return
    lines = []
    for d in dialogs:
        mark = "•"
        if active and d.id == active.id:
            mark = "▶"
        title = (d.title or "").strip() or "(без названия)"
        lines.append(f"{mark} {d.id}: {title}")
    await update.message.reply_text("Ваши диалоги:\n" + "\n".join(lines))

async def cmd_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    ds: DialogService = context.bot_data.get("svc_dialog")
    if not ds or not update.effective_user:
        await update.message.reply_text("⚠️ Сервис диалогов не настроен.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /dialog <id>")
        return
    try:
        did = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID диалога должен быть числом.")
        return
    ok = ds.switch_dialog(update.effective_user.id, did)
    if not ok:
        await update.message.reply_text("Диалог не найден.")
        return
    await update.message.reply_text(f"Переключено на диалог {did}.")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    ds: DialogService = context.bot_data.get("svc_dialog")
    if not ds or not update.effective_user:
        await update.message.reply_text("⚠️ Сервис диалогов не настроен.")
        return
    d = ds.new_dialog(update.effective_user.id, title="")
    await update.message.reply_text(f"Создан новый диалог: {d.id}")

async def cmd_dialog_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Alias for /reset
    await cmd_reset(update, context)

def register(app: Application) -> None:
    app.add_handler(CommandHandler("dialogs", cmd_dialogs))
    app.add_handler(CommandHandler("dialog", cmd_dialog))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("dialog_new", cmd_dialog_new))
