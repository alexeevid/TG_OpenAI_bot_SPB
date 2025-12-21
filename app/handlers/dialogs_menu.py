from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler
from telegram.constants import ParseMode

from app.db.repo_dialogs import DialogsRepo

def build_dialogs_list_menu(dialogs, active_dialog_id):
    keyboard = []
    for d in dialogs:
        title = d.title or 'Без имени'
        label = f"\u2705 {title}" if d.id == active_dialog_id else title
        keyboard.append([
            InlineKeyboardButton(text=label, callback_data=f"select:{d.id}")
        ])
    return InlineKeyboardMarkup(keyboard)


def build_dialog_options_menu(dialog_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Переименовать", callback_data=f"rename:{dialog_id}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"delete:{dialog_id}"),
            InlineKeyboardButton("⭐ Сделать активным", callback_data=f"setactive:{dialog_id}")
        ]
    ])


async def show_dialogs_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    user_id = update.effective_user.id
    dialogs = repo.list_dialogs(user_id)
    user = repo.get_user(user_id)
    if not dialogs:
        await update.message.reply_text("У вас пока нет диалогов.")
        return

    menu = build_dialogs_list_menu(dialogs, user.active_dialog_id if user else None)
    await update.message.reply_text("Выберите диалог:", reply_markup=menu)


async def handle_dialogs_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("select:"):
        dialog_id = int(data.split(":")[1])
        context.user_data["selected_dialog_id"] = dialog_id
        await query.message.reply_text("Что сделать с диалогом?", reply_markup=build_dialog_options_menu(dialog_id))

    elif data.startswith("rename:"):
        dialog_id = int(data.split(":")[1])
        context.user_data["rename_dialog_id"] = dialog_id
        await query.message.reply_text("Введите новое имя для диалога:", reply_markup={"force_reply": True})

    elif data.startswith("delete:"):
        dialog_id = int(data.split(":")[1])
        repo: DialogsRepo = context.bot_data["repo_dialogs"]
        repo.delete_dialog(dialog_id)
        await query.message.reply_text("🗑 Диалог удалён.")
        await show_dialogs_list(update, context)

    elif data.startswith("setactive:"):
        dialog_id = int(data.split(":")[1])
        repo: DialogsRepo = context.bot_data["repo_dialogs"]
        repo.set_active_dialog(update.effective_user.id, dialog_id)
        await query.message.reply_text("⭐ Активный диалог обновлён.")
        await show_dialogs_list(update, context)


def register(app) -> None:
    app.add_handler(CommandHandler("menu", show_dialogs_list))
    app.add_handler(CallbackQueryHandler(handle_dialogs_click, pattern=r"^(select|rename|delete|setactive):"))
