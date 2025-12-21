from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters

from app.db.repo_dialogs import DialogsRepo


def build_dialogs_menu(dialogs: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(dialog_name or f"Диалог {dialog_id}", callback_data=f"rename:{dialog_id}")]
        for dialog_id, dialog_name in dialogs
    ]
    return InlineKeyboardMarkup(buttons)


async def show_dialogs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    user_id = update.effective_user.id
    dialogs = repo.list_dialogs(user_id)
    if not dialogs:
        await update.message.reply_text("У вас пока нет диалогов.")
        return

    menu = build_dialogs_menu([(d.id, d.title) for d in dialogs])
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
        reply_markup=ForceReply(selective=True)
    )


async def handle_rename_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    dialog_id = context.user_data.pop("rename_dialog_id", None)
    if not dialog_id:
        return

    new_title = update.message.text.strip()
    if not new_title:
        await update.message.reply_text("⚠️ Имя не может быть пустым.")
        return

    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    repo.rename_dialog(dialog_id, new_title)

    await update.message.reply_text(f"✅ Диалог переименован в «{new_title}».")


def register(app) -> None:
    app.add_handler(CallbackQueryHandler(handle_dialogs_menu_click, pattern=r"^rename:\d+$"))
    app.add_handler(app.command_handler("menu", show_dialogs_menu))
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, handle_rename_reply))
