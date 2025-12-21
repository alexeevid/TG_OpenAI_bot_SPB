from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler

from app.db.repo_dialogs import DialogsRepo

PAGE_SIZE = 3

def build_dialogs_menu(dialogs, active_dialog_id, page=0):
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    visible_dialogs = dialogs[start:end]

    keyboard = []
    for d in visible_dialogs:
        title = d.title or f"Диалог {d.id}"
        short_title = title if len(title) < 30 else title[:27] + "..."

        row_title = [
            InlineKeyboardButton(
                text=f"\U0001F4DC {short_title}",  # 📝
                callback_data=f"noop:{d.id}"
            )
        ]
        row_buttons = [
            InlineKeyboardButton("✏️", callback_data=f"rename:{d.id}"),
            InlineKeyboardButton("🗑", callback_data=f"delete:{d.id}"),
            InlineKeyboardButton("⭐" if d.id == active_dialog_id else "☆", callback_data=f"setactive:{d.id}")
        ]
        keyboard.append(row_title)
        keyboard.append(row_buttons)

    # Page indicator and nav
    keyboard.append([InlineKeyboardButton(f"Показано {min(end, len(dialogs))} из {len(dialogs)}", callback_data="noop:-1")])

    if end < len(dialogs):
        keyboard.append([InlineKeyboardButton("➡️ Вперёд", callback_data=f"page:{page + 1}")])
    elif page > 0:
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{page - 1}")])

    return InlineKeyboardMarkup(keyboard)


async def show_dialogs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    user_id = update.effective_user.id
    dialogs = sorted(repo.list_dialogs(user_id), key=lambda d: d.updated_at or d.created_at, reverse=True)
    user = repo.get_user(user_id)

    if not dialogs:
        await update.message.reply_text("У вас пока нет диалогов.")
        return

    context.user_data["menu_page"] = page
    menu = build_dialogs_menu(dialogs, user.active_dialog_id if user else None, page=page)

    if update.message:
        await update.message.reply_text("Выберите диалог:", reply_markup=menu)
    elif update.callback_query:
        await update.callback_query.edit_message_reply_markup(reply_markup=menu)


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
        await query.message.reply_text("🗑 Диалог удалён.")
        await show_dialogs_menu(update, context, page=context.user_data.get("menu_page", 0))

    elif data.startswith("setactive:"):
        dialog_id = int(data.split(":")[1])
        repo: DialogsRepo = context.bot_data["repo_dialogs"]
        repo.set_active_dialog(update.effective_user.id, dialog_id)
        await query.message.reply_text("⭐ Активный диалог обновлён.")
        await show_dialogs_menu(update, context, page=context.user_data.get("menu_page", 0))

    elif data.startswith("page:"):
        page = int(data.split(":")[1])
        await show_dialogs_menu(update, context, page=page)


def register(app) -> None:
    app.add_handler(CommandHandler("menu", show_dialogs_menu))
    app.add_handler(CallbackQueryHandler(handle_dialogs_menu_click, pattern=r"^(rename|delete|setactive|noop|page):"))
