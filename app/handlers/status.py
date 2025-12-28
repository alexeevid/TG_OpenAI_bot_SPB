from telegram import Update
from telegram.ext import ContextTypes

from ..services.authz_service import AuthzService
from ..services.dialog_service import DialogService


def _fmt_dt(dt) -> str:
    try:
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "-"


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return

    ds: DialogService = context.bot_data.get("svc_dialog")
    cfg = context.bot_data.get("settings")
    if not ds or not cfg or not update.effective_user:
        await update.message.reply_text("⚠️ Сервисы не настроены.")
        return

    d = ds.get_active_dialog(update.effective_user.id)
    s = ds.get_active_settings(update.effective_user.id) or {}

    model = s.get("model") or getattr(cfg, "openai_chat_model", "unknown")
    mode = s.get("mode") or "default"
    image_enabled = bool(s.get("image_enabled", True))
    rag_enabled = bool(s.get("rag_enabled", False))

    history = ds.history(d.id, limit=500)
    total = len(history)
    user_count = sum(1 for m in history if getattr(m, "role", "") == "user")
    assistant_count = sum(1 for m in history if getattr(m, "role", "") == "assistant")

    created = _fmt_dt(getattr(d, "created_at", None))
    updated = _fmt_dt(getattr(d, "updated_at", None))

    text = (
        f"📄 Диалог: {d.id} — {d.title or '(без названия)'}\n"
        f"📅 Создан: {created}\n"
        f"⌛ Последнее изменение: {updated}\n"
        f"🤖 Модель: {model}  |  🎯 Режим: {mode}\n"
        f"💬 Сообщений: {total} (пользователь: {user_count}, ассистент: {assistant_count})\n"
        f"🖼️ Генерация изображений: {'включена' if image_enabled else 'отключена'}\n"
        f"📚 База знаний (RAG): {'включена' if rag_enabled else 'отключена'}"
    )
    await update.message.reply_text(text)


def register(app):
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler(["status", "stats"], cmd_status))
