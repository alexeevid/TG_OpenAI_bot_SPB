async def dialog_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите ID диалога. Например: /dialog 123")
        return
    dialog_id = context.args[0]
    success = dialog_manager.switch_dialog(update.effective_user.id, dialog_id)
    if success:
        await update.message.reply_text(f"Переключено на диалог {dialog_id}")
    else:
        await update.message.reply_text("Диалог не найден.")
