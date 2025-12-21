from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from ..services.dialog_service import DialogService
from ..services.authz_service import AuthzService
from ..db.models import Dialog

# Вспомогательная функция для построения меню диалогов
def _build_dialogs_menu(user_id: int, ds: DialogService) -> InlineKeyboardMarkup:
    dialogs = ds.list_dialogs(user_id, limit=20)
    active = ds.get_active_dialog(user_id)
    if not dialogs:
        # Если у пользователя еще нет диалогов – показываем только кнопку создания нового
        return InlineKeyboardMarkup([[InlineKeyboardButton("➕ Новый диалог", callback_data="new_dialog")]])
    keyboard = []
    for d in dialogs:
        title = (d.title or "").strip() or "(без названия)"
        mark = "▶" if active and d.id == active.id else "•"
        text = f"{mark} {d.id}: {title}"
        keyboard.append([
            InlineKeyboardButton(text, callback_data=f"switch_dialog:{d.id}"),
            InlineKeyboardButton("❌", callback_data=f"delete_dialog:{d.id}")
        ])
    # Кнопка для создания нового диалога (всегда в конце списка)
    keyboard.append([InlineKeyboardButton("➕ Новый диалог", callback_data="new_dialog")])
    return InlineKeyboardMarkup(keyboard)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Хендлер команды /menu: выводит меню управления диалогами с кнопками."""
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    ds: DialogService = context.bot_data.get("svc_dialog")
    if not ds or not update.effective_user:
        await update.message.reply_text("⚠️ Сервис диалогов не настроен.")
        return
    user_id = update.effective_user.id
    # Если диалогов нет, сразу предлагаем создать новый
    dialogs = ds.list_dialogs(user_id, limit=20)
    if not dialogs:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Новый диалог", callback_data="new_dialog")]])
        await update.message.reply_text("Диалогов пока нет.", reply_markup=markup)
        return
    # Иначе показываем меню со списком диалогов
    menu_markup = _build_dialogs_menu(user_id, ds)
    await update.message.reply_text("Ваши диалоги:", reply_markup=menu_markup)

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Хендлер для всех нажатий кнопок в меню диалогов (/menu)."""
    query = update.callback_query
    if not query:
        return
    # Ответим сразу на запрос (убирает «часики» у кнопки)
    await query.answer()
    az: AuthzService = context.bot_data.get("svc_authz")
    user = query.from_user
    if az and user and not az.is_allowed(user.id):
        await query.answer("⛔ Доступ запрещен.", show_alert=True)
        return
    ds: DialogService = context.bot_data.get("svc_dialog")
    if not ds or not user:
        await query.answer("⚠️ Сервис диалогов не настроен.", show_alert=True)
        return
    user_id = user.id
    data = query.data or ""
    if data == "new_dialog":
        # Создаем новый диалог (автоматически становится активным)
        ds.new_dialog(user_id, title="")
        await query.answer("✅ Новый диалог создан.", show_alert=False)
    elif data.startswith("switch_dialog:"):
        try:
            dialog_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("❌ Некорректный запрос.", show_alert=True)
            return
        ok = ds.switch_dialog(user_id, dialog_id)
        if not ok:
            await query.answer("❌ Диалог не найден.", show_alert=True)
            return
        await query.answer("✅ Диалог переключен.", show_alert=False)
    elif data.startswith("delete_dialog:"):
        try:
            dialog_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("❌ Некорректный запрос.", show_alert=True)
            return
        # Удаляем указанный диалог пользователя из базы данных
        repo = context.bot_data.get("repo_dialogs")
        user_db = repo.ensure_user(str(user_id)) if repo else None
        dialog_obj = repo.get_dialog_for_user(dialog_id, user_db.id) if repo and user_db else None
        if not dialog_obj:
            await query.answer("❌ Диалог не найден.", show_alert=True)
            return
        with repo.sf() as session:
            d = session.get(Dialog, dialog_obj.id)
            if d:
                session.delete(d)
                session.commit()
        await query.answer("✅ Диалог удален.", show_alert=False)
    # Обновляем сообщение меню, чтобы отразить изменения (переключение/удаление/добавление диалога)
    new_menu = _build_dialogs_menu(user_id, ds)
    if len(new_menu.inline_keyboard) == 1 and new_menu.inline_keyboard[0][0].callback_data == "new_dialog":
        # Если диалогов больше не осталось – меняем текст и показываем только кнопку «Новый диалог»
        await query.edit_message_text("Диалогов больше нет.", reply_markup=new_menu)
    else:
        await query.edit_message_text("Ваши диалоги:", reply_markup=new_menu)

def register(app: Application) -> None:
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^(switch_dialog|delete_dialog|new_dialog)"))
