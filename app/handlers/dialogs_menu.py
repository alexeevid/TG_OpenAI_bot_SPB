from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler
from telegram.constants import ParseMode

from app.db.repo_dialogs import DialogsRepo


def build_dialogs_menu(dialogs, active_dialog_id):
    keyboard = []
    for d in dialogs[:3]:  # Ограничим до 3 диалогов
        title = d.title or "Без имени"
        keyboard.append([
            InlineKeyboardButton(f"🧾 {title}", callback_data=f"noop:{d.id}")
        ])
        keyboard.append([
            InlineKeyboardButton("✏️", callback_data=f"rename:{d.id}"),
            InlineKeyboardButton("🗑", callback_data=f"delete:{d.id}"),
            InlineKeyboardButton(
                "⭐" if d.id == active_dialog_id else "☆",
                callback_data=f"setactive:{d.id}"
            )
        ])
    return InlineKeyboardMarkup(keyboard)


async def show_dialogs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False) -> None:
    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    user_id = update.effective_user.id
    dialogs = repo.list_dialogs(user_id)
    user = repo.get_user(user_id)

    if not dialogs:
        await update.message.reply_text("У вас пока нет диалогов.")
        return

    menu = build_dialogs_menu(dialogs, user.active_dialog_id if user else None)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text="Выберите диалог:",
            reply_markup=menu
        )
    else:
        await update.message.reply_text("Выберите диалог:", reply_markup=menu)


async def handle_dialogs_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    repo: DialogsRepo = context.bot_data["repo_dialogs"]

    if data.startswith("rename:"):
        dialog_id = int(data.split(":")[1])
        context.user_data["rename_dialog_id"] = dialog_id
        await query.message.reply_text("Введите новое имя для диалога:", reply_markup={"force_reply": True})

    elif data.startswith("delete:"):
        dialog_id = int(data.split(":")[1])
        repo.delete_dialog(dialog_id)
        await query.message.reply_text("🗑 Диалог удалён.")
        await show_dialogs_menu(update, context, edit=True)

    elif data.startswith("setactive:"):
        dialog_id = int(data.split(":")[1])
        repo.set_active_dialog(user_id, dialog_id)
        await query.message.reply_text("⭐ Активный диалог обновлён.")
        await show_dialogs_menu(update, context, edit=True)

    elif data.startswith("noop:"):
        # Специальная заглушка, чтобы не делать ничего при клике на заголовок
        pass


def register(app) -> None:
    app.add_handler(CommandHandler("menu", show_dialogs_menu))
    app.add_handler(CallbackQueryHandler(handle_dialogs_menu_click, pattern=r"^(rename|delete|setactive|noop):"))
