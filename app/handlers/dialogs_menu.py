from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler

from app.db.repo_dialogs import DialogsRepo


def build_dialogs_menu(dialogs, active_dialog_id):
    keyboard = []
    for d in dialogs[:5]:
        title_text = f"🧾 {d.title[:30]}" if d.title else "🧾 Без имени"
        # Название на первой строке
        keyboard.append([
            InlineKeyboardButton(
                text=title_text,
                callback_data=f"noop:{d.id}"
            )
        ])
        # Управление на второй строке
        keyboard.append([
            InlineKeyboardButton("✏️", callback_data=f"rename:{d.id}"),
            InlineKeyboardButton("🗑", callback_data=f"confirmdelete:{d.id}"),
            InlineKeyboardButton(
                "⭐" if d.id == active_dialog_id else "☆",
                callback_data=f"setactive:{d.id}"
            )
        ])
    return InlineKeyboardMarkup(keyboard)


async def show_dialogs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: DialogsRepo = context.bot_data["repo_dialogs"]
    user_id = update.effective_user.id
    dialogs = repo.list_dialogs(user_id)
    user = repo.get_user(user_id)
    if not dialogs:
        await update.message.reply_text("У вас пока нет диалогов.")
        return

    menu = build_dialogs_menu(dialogs, user.active_dialog_id if user else None)
    await update.message.reply_text("Выберите диалог:", reply_markup=menu)


async def handle_dialogs_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    repo: DialogsRepo = context.bot_data["repo_dialogs"]

    if data.startswith("rename:"):
        dialog_id = int(data.split(":")[1])
        context.user_data["rename_dialog_id"] = dialog_id
        await query.message.reply_text("Введите новое имя для диалога:", reply_markup={"force_reply": True})

    elif data.startswith("confirmdelete:"):
        dialog_id = int(data.split(":")[1])
        keyboard = [
            [
                InlineKeyboardButton("✅ Да, удалить", callback_data=f"delete:{dialog_id}"),
                InlineKeyboardButton("↩️ Отмена", callback_data="cancel")
            ]
        ]
        await query.message.edit_text("Вы уверены, что хотите удалить диалог?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("delete:"):
        dialog_id = int(data.split(":")[1])
        repo.delete_dialog(dialog_id)
        await query.message.edit_text("🗑 Диалог удалён.")
        await show_dialogs_menu(update, context)

    elif data.startswith("setactive:"):
        dialog_id = int(data.split(":")[1])
        repo.set_active_dialog(update.effective_user.id, dialog_id)
        await query.message.edit_text("⭐ Активный диалог обновлён.")
        await show_dialogs_menu(update, context)

    elif data == "cancel":
        await show_dialogs_menu(update, context)


def register(app) -> None:
    app.add_handler(CommandHandler("menu", show_dialogs_menu))
    app.add_handler(CallbackQueryHandler(handle_dialogs_menu_click, pattern=r"^(rename|delete|setactive|confirmdelete|cancel|noop):"))
