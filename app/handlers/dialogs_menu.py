from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler

from app.db.repo_dialogs import DialogsRepo
from app.db.models import Dialog

def build_dialogs_menu(dialogs: list[Dialog]) -> InlineKeyboardMarkup:
    buttons = []
    for dialog in dialogs[:5]:  # показываем только 5 последних
        title = dialog.title or f"Диалог {dialog.id}"
        row = [
            [InlineKeyboardButton(f"📄 {title}", callback_data=f"noop:{dialog.id}")],
            [
                InlineKeyboardButton("✏️", callback_data=f"rename:{dialog.id}"),
                InlineKeyboardButton("🗑", callback_data=f"delete:{dialog.id}"),
                InlineKeyboardButton("⭐", callback_data=f"activate:{dialog.id}"),
            ]
        ]
        buttons.extend(row)
    return InlineKeyboardMarkup(buttons)


async def show_dialogs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    user_id = update.effective_user.id
    dialogs = repo.list_dialogs(user_id)
    if not dialogs:
        await update.message.reply_text("У вас пока нет диалогов.")
        return

    menu = build_dialogs_menu(dialogs)
    await update.message.reply_text("Выберите диалог:", reply_markup=menu)


async def handle_dialogs_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("rename:"):
        dialog_id = int(data.split(":")[1])
        context.user_data["rename_dialog_id"] = dialog_id
        await query.message.reply_text("Введите новое имя для диалога:", reply_markup={"force_reply": True})
    elif data.startswith("delete:"):
        dialog_id = int(data.split(":")[1])
        repo: DialogsRepo = context.bot_data["repo_dialogs"]
        repo.delete_dialog(dialog_id)
        await query.message.reply_text("Диалог удалён. Используйте /menu для обновления.")
    elif data.startswith("activate:"):
        dialog_id = int(data.split(":")[1])
        repo: DialogsRepo = context.bot_data["repo_dialogs"]
        repo.set_active_dialog(update.effective_user.id, dialog_id)
        await query.message.reply_text("Диалог активирован. Можете продолжать общение.")
    # "noop" — фиктивный обработчик, чтобы не падало при клике на заголовок


def register(app) -> None:
    app.add_handler(CallbackQueryHandler(handle_dialogs_menu_click, pattern=r"^(rename|delete|activate|noop):\\d+$"))
    app.add_handler(CommandHandler("menu", show_dialogs_menu))
