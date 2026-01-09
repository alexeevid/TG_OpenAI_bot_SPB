from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from ..core.response_modes import MODE_TITLES, normalize_mode
from ..core.utils import with_mode_prefix
from ..services.dialog_service import DialogService
from ..services.authz_service import AuthzService

# В /mode показываем ключевые режимы.
MODES = [
    ("no_accent", MODE_TITLES["no_accent"]),
    ("seo", MODE_TITLES["seo"]),
    ("professional", MODE_TITLES["professional"]),
    ("trainer", MODE_TITLES["trainer"]),
    ("simple", MODE_TITLES["simple"]),
]


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text(with_mode_prefix(context, update.effective_user.id, "⛔ Доступ запрещен."))
        return

    if not update.message:
        return

    ds: DialogService = context.bot_data.get("svc_dialog")
    if not ds or not update.effective_user:
        uid = update.effective_user.id if update.effective_user else None
        await update.message.reply_text(with_mode_prefix(context, uid, "⚠️ Сервис диалогов не настроен."))
        return

    settings = ds.get_active_settings(update.effective_user.id) or {}
    current = normalize_mode(str(settings.get("mode") or "")) or "professional"

    rows = []
    for key, title in MODES:
        mark = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(f"{mark}{title}", callback_data=f"mode|{key}")])

    kb = InlineKeyboardMarkup(rows)
    await update.message.reply_text(with_mode_prefix(context, update.effective_user.id, "Выберите режим ответов:"), reply_markup=kb)


async def on_mode_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return

    az: AuthzService = context.bot_data.get("svc_authz")
    if az and q.from_user and not az.is_allowed(q.from_user.id):
        await q.answer("⛔ Доступ запрещен.", show_alert=True)
        return

    try:
        await q.answer()
    except Exception:
        pass

    data = (q.data or "")
    if not data.startswith("mode|"):
        return

    mode = data.split("|", 1)[1].strip()
    mode = normalize_mode(mode)

    ds: DialogService = context.bot_data.get("svc_dialog")
    if not ds or not q.from_user:
        uid = q.from_user.id if q.from_user else None
        await q.edit_message_text(with_mode_prefix(context, uid, "⚠️ Сервис диалогов не настроен."))
        return

    ds.update_active_settings(q.from_user.id, {"mode": mode})

    title = MODE_TITLES.get(mode, mode)
    await q.edit_message_text(with_mode_prefix(context, q.from_user.id, f"✅ Режим для диалога установлен: {title}"))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CallbackQueryHandler(on_mode_cb, pattern=r"^mode\|"))
