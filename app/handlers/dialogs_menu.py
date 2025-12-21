from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler
from app.db.repo_dialogs import DialogsRepo


def build_dialogs_menu(dialogs: list) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(dialog.title or f"Диалог {dialog.id}", callback_data=f"rename:{dialog.id}")]
        for dialog in dialogs
    ]
    return InlineKeyboardMarkup(buttons)


async def show_dialogs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    user_id = update.effective_user.id
    dialogs = repo.list_dialogs(user_id)
    if not dialogs:
        await update.message.reply_text("У вас пока нет диалогов.")
        return

    menu = build_dialogs_menu(dialogs)
    await update.message.reply_text("Выберите диалог для переименования:", reply_markup=menu)


async def handle_dialogs_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("rename:"):
        return

    dialog_id = int(query.data.split(":")[1])
    context.user_data["rename_dialog_id"] = dialog_id

    await query.message.reply_text(
        "Введите новое имя для диалога:",
        reply_markup=None,
        reply_to_message_id=query.message.message_id,
        allow_sending_without_reply=True
    )


def register(app) -> None:
    app.add_handler(CallbackQueryHandler(handle_dialogs_menu_click, pattern=r"^rename:\d+$"))
    app.add_handler(CommandHandler("menu", show_dialogs_menu))
