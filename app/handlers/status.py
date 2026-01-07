from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from ..services.authz_service import AuthzService
from ..services.dialog_service import DialogService
from ..services.dialog_kb_service import DialogKBService


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await msg.reply_text("⛔ Доступ запрещен.")
        return

    ds: DialogService = context.bot_data.get("svc_dialog")
    cfg = context.bot_data.get("settings")
    if not ds or not cfg or not update.effective_user:
        await msg.reply_text("⚠️ Сервисы не настроены.")
        return

    # Надёжно: гарантируем активный диалог
    d = ds.ensure_active_dialog(update.effective_user.id)
    settings = ds.get_active_settings(update.effective_user.id) or {}

    # Информация по диалогу
    mode = str(settings.get("mode") or "detailed")

    # Модели по модальностям (источник истины — settings диалога)
    # Если вдруг пусто — показываем дефолты из cfg для понятности.
    text_model = str(settings.get("text_model") or getattr(cfg, "text_model", "unknown"))
    image_model = str(settings.get("image_model") or getattr(cfg, "image_model", "unknown"))
    transcribe_model = str(settings.get("transcribe_model") or getattr(cfg, "transcribe_model", "unknown"))

    image_enabled = bool(context.bot_data.get("svc_image"))
    rag_enabled = bool(context.bot_data.get("svc_rag"))

    # KB scope (режим и количество документов в текущем диалоге)
    kb_mode = "-"
    kb_enabled_docs = 0
    kb_attached_docs = 0
    dkb: DialogKBService | None = context.bot_data.get("svc_dialog_kb")
    if dkb:
        try:
            kb_mode = dkb.get_mode(d.id)
            attached = dkb.list_attached(d.id) or []
            kb_attached_docs = len(attached)
            kb_enabled_docs = sum(1 for x in attached if bool(x.get("is_enabled")))
        except Exception:
            # статус — не критичен, не ломаем команду
            pass

    history = ds.history(d.id, limit=1000)
    total = len(history)
    user_count = sum(1 for m in history if getattr(m, "role", "") == "user")
    assistant_count = sum(1 for m in history if getattr(m, "role", "") == "assistant")

    created_at = getattr(d, "created_at", None)
    updated_at = getattr(d, "updated_at", None)
    created_s = created_at.strftime("%d.%m.%Y %H:%M") if created_at else "-"
    updated_s = updated_at.strftime("%d.%m.%Y %H:%M") if updated_at else "-"

    text = (
        f"📄 Диалог: {d.id} — {d.title or '(без названия)'}\n"
        f"🕒 Создан: {created_s}  |  ✏️ Изменён: {updated_s}\n"
        f"🎯 Режим: {mode}\n"
        f"🤖 Модели:\n"
        f"  • Текст: {text_model}\n"
        f"  • Изображения: {image_model}\n"
        f"  • Распознавание: {transcribe_model}\n"
        f"💬 Сообщений: {total} (пользователь: {user_count}, ассистент: {assistant_count})\n"
        f"🖼️ Генерация изображений: {'включена' if image_enabled else 'отключена'}\n"
        f"📚 База знаний (RAG): {'включена' if rag_enabled else 'отключена'}\n"
        f"   • KB mode: {kb_mode}\n"
        f"   • Документы: подключено {kb_attached_docs}, включено {kb_enabled_docs}"
    )
    await msg.reply_text(text)


def register(app):
    from telegram.ext import CommandHandler

    app.add_handler(CommandHandler("status", cmd_status))
    # Совместимость: команда /stats в меню
    app.add_handler(CommandHandler("stats", cmd_status))
