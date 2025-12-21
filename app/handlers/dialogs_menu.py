from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler
from telegram.constants import ParseMode

from app.db.repo_dialogs import DialogsRepo
from datetime import datetime

def format_dialog_title(dialog):
    created_date = dialog.created_at.strftime("%Y-%m-%d") if dialog.created_at else "????-??-??"
    title = dialog.title or "Без имени"
    return f"{created_date}_{title}"

def build_dialogs_menu(dialogs, active_dialog_id, offset=0, page_size=3):
    keyboard = []
    page_dialogs = dialogs[offset:offset + page_size]

    for d in page_dialogs:
        title_row = [
            InlineKeyboardButton(
                text=format_dialog_title(d),
                callback_data=f"noop:{d.id}"
            )
        ]
        button_row = [
            InlineKeyboardButton("✏️", callback_data=f"rename:{d.id}"),
            InlineKeyboardButton("🗑", callback_data=f"delete:{d.id}"),
            InlineKeyboardButton(
                "⭐" if d.id == active_dialog_id else "☆",
                callback_data=f"setactive:{d.id}"
            )
        ]
        keyboard.append(title_row)
        keyboard.append(button_row)

    # Pagination controls
    nav_buttons = []
    if offset > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"page:{offset - page_size}"))
    if offset + page_size < len(dialogs):
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"page:{offset + page_size}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    return InlineKeyboardMarkup(keyboard)


async def show_dialogs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, offset: int = 0) -> None:
    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    user_id = update.effective_user.id
    dialogs = repo.list_dialogs(user_id)
    user = repo.get_user(user_id)
    if not dialogs:
        await update.message.reply_text("У вас пока нет диалогов.")
        return

    dialogs.sort(key=lambda d: d.updated_at or d.created_at, reverse=True)
    menu = build_dialogs_menu(dialogs, user.active_dialog_id if user else None, offset)
    await update.message.reply_text("Ваши диалоги:", reply_markup=menu)


async def handle_dialogs_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    user_id = update.effective_user.id

    if data.startswith("rename:"):
        dialog_id = int(data.split(":")[1])
        context.user_data["rename_dialog_id"] = dialog_id
        await query.message.reply_text("Введите новое имя для диалога:", reply_markup={"force_reply": True})

    elif data.startswith("delete:"):
        dialog_id = int(data.split(":")[1])
        repo.delete_dialog(dialog_id)
        await query.message.reply_text("🗑 Диалог удалён.")
        await show_dialogs_menu(update, context)

    elif data.startswith("setactive:"):
        dialog_id = int(data.split(":")[1])
        repo.set_active_dialog(user_id, dialog_id)
        await query.message.reply_text("⭐ Активный диалог обновлён.")
        await show_dialogs_menu(update, context)

    elif data.startswith("page:"):
        offset = int(data.split(":")[1])
        await show_dialogs_menu(update, context, offset)


def register(app) -> None:
    app.add_handler(CommandHandler("menu", show_dialogs_menu))
    app.add_handler(CallbackQueryHandler(handle_dialogs_menu_click, pattern=r"^(rename|delete|setactive|noop|page):"))
