from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler
from app.db.repo_dialogs import DialogsRepo
from datetime import datetime

def dialog_button_label(dialog, is_active: bool) -> str:
    date_part = dialog.created_at.strftime("%Y-%m-%d")
    title_part = dialog.title or f"Диалог {dialog.id}"
    prefix = "⭐ " if is_active else ""
    return f"{prefix}{date_part} {title_part}"

def build_dialogs_menu(dialogs: list, active_id: int) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for idx, dialog in enumerate(dialogs):
        label = dialog_button_label(dialog, dialog.id == active_id)
        row.append(InlineKeyboardButton(label, callback_data=f"activate:{dialog.id}"))
        row.append(InlineKeyboardButton("✏️", callback_data=f"rename:{dialog.id}"))
        row.append(InlineKeyboardButton("🗑", callback_data=f"delete:{dialog.id}"))
        if len(row) == 6:  # two dialogs per row (3 buttons each)
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


async def show_dialogs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    user_id = update.effective_user.id
    dialogs = repo.list_dialogs(user_id)
    if not dialogs:
        await update.message.reply_text("У вас пока нет диалогов.")
        return

    active = repo.get_active_dialog(user_id)
    active_id = active.id if active else -1
    menu = build_dialogs_menu(dialogs, active_id)
    await update.message.reply_text("Выберите диалог:", reply_markup=menu)


async def handle_dialogs_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not query.data:
        return

    action, dialog_id = query.data.split(":")
    dialog_id = int(dialog_id)
    context.user_data["selected_dialog_id"] = dialog_id

    if action == "rename":
        await query.message.reply_text("Введите новое имя для диалога:", reply_markup={"force_reply": True})
    elif action == "delete":
        repo: DialogsRepo = context.bot_data["repo_dialogs"]
        repo.delete_dialog(dialog_id)
        await query.message.reply_text("Диалог удален.")
        await show_dialogs_menu(update, context)
    elif action == "activate":
        repo: DialogsRepo = context.bot_data["repo_dialogs"]
        user_id = update.effective_user.id
        repo.set_active_dialog(user_id, dialog_id)
        await query.message.reply_text("Диалог активирован.")
        await show_dialogs_menu(update, context)

def register(app) -> None:
    app.add_handler(CallbackQueryHandler(handle_dialogs_menu_click, pattern=r"^(rename|delete|activate):\\d+$"))
    app.add_handler(CommandHandler("menu", show_dialogs_menu))
