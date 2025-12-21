from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler
from telegram.constants import ParseMode

from app.db.repo_dialogs import DialogsRepo

DIALOGS_PER_PAGE = 3

def build_dialogs_menu(dialogs, active_dialog_id, page: int, total_count: int) -> InlineKeyboardMarkup:
    keyboard = []
    start = page * DIALOGS_PER_PAGE
    end = start + DIALOGS_PER_PAGE
    page_dialogs = dialogs[start:end]

    for d in page_dialogs:
        title = d.title or 'Без имени'
        row_title = [
            InlineKeyboardButton(
                text=f"🧾 {title}",
                callback_data=f"noop:{d.id}"
            )
        ]
        row_buttons = [
            InlineKeyboardButton("✏️", callback_data=f"rename:{d.id}"),
            InlineKeyboardButton("🗑", callback_data=f"delete:{d.id}"),
            InlineKeyboardButton(
                "⭐" if d.id == active_dialog_id else "☆",
                callback_data=f"setactive:{d.id}"
            )
        ]
        keyboard.append(row_title)
        keyboard.append(row_buttons)

    keyboard.append([
        InlineKeyboardButton(f"Показано {min(end, total_count)} из {total_count}", callback_data="noop:info")
    ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{page - 1}"))
    if end < total_count:
        nav_buttons.append(InlineKeyboardButton("➡️ Вперёд", callback_data=f"page:{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    return InlineKeyboardMarkup(keyboard)


async def show_dialogs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    user_id = update.effective_user.id
    dialogs = repo.list_dialogs(user_id)
    user = repo.get_user(user_id)
    total_count = len(dialogs)
    if not dialogs:
        await update.message.reply_text("У вас пока нет диалогов.")
        return

    context.user_data["dialog_page"] = page
    menu = build_dialogs_menu(dialogs, user.active_dialog_id if user else None, page, total_count)
    await update.message.reply_text("Выберите диалог:", reply_markup=menu)


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
        await show_dialogs_menu(update, context, context.user_data.get("dialog_page", 0))

    elif data.startswith("setactive:"):
        dialog_id = int(data.split(":")[1])
        repo.set_active_dialog(user_id, dialog_id)
        await query.message.reply_text("⭐ Активный диалог обновлён.")
        await show_dialogs_menu(update, context, context.user_data.get("dialog_page", 0))

    elif data.startswith("page:"):
        page = int(data.split(":")[1])
        await show_dialogs_menu(update, context, page)


def register(app) -> None:
    app.add_handler(CommandHandler("menu", show_dialogs_menu))
    app.add_handler(CallbackQueryHandler(handle_dialogs_menu_click, pattern=r"^(rename|delete|setactive|page|noop):"))
