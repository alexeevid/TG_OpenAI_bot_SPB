from __future__ import annotations

import re
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.services.dialog_service import DialogService
from app.utils.auth import require_auth


STATE_RENAME = 1


# =========================
# Callbacks (UI)
# =========================

@require_auth
async def cb_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    svc: DialogService = context.application.bot_data["svc_dialog"]
    user_id = update.effective_user.id

    m = re.match(r"^dlg:(\w+)(?::(\d+))?$", query.data)
    if not m:
        return

    action, dlg_id = m.group(1), m.group(2)
    dlg_id = int(dlg_id) if dlg_id else None

    if action == "open" and dlg_id:
        await svc.open_dialog(user_id, dlg_id, query)

    elif action == "delete" and dlg_id:
        await svc.ask_delete_dialog(user_id, dlg_id, query)

    elif action == "delete_ok" and dlg_id:
        await svc.delete_dialog(user_id, dlg_id, query)

    elif action == "new":
        await svc.create_dialog(user_id, query)

    elif action == "page":
        await svc.page_dialogs(user_id, query)

    elif action == "refresh":
        await svc.render_dialogs(user_id, query)

    elif action == "cancel":
        await svc.render_dialogs(user_id, query)

    elif action == "close":
        await query.message.delete()

    elif action == "noop":
        return


# =========================
# Rename flow (Conversation)
# =========================

@require_auth
async def cb_rename_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    svc: DialogService = context.application.bot_data["svc_dialog"]
    user_id = update.effective_user.id

    m = re.match(r"^dlg:rename:(\d+)$", query.data)
    if not m:
        return ConversationHandler.END

    dlg_id = int(m.group(1))
    context.user_data["rename_dlg_id"] = dlg_id

    await svc.ask_rename_dialog(user_id, dlg_id, query)
    return STATE_RENAME


@require_auth
async def cb_rename_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    svc: DialogService = context.application.bot_data["svc_dialog"]
    user_id = update.effective_user.id

    dlg_id: Optional[int] = context.user_data.get("rename_dlg_id")
    if not dlg_id:
        return ConversationHandler.END

    new_name = update.message.text.strip()
    await svc.rename_dialog(user_id, dlg_id, new_name, update)

    context.user_data.pop("rename_dlg_id", None)
    return ConversationHandler.END


@require_auth
async def cb_rename_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("rename_dlg_id", None)
    return ConversationHandler.END


# =========================
# Registration
# =========================

def register(app: Application) -> None:
    """
    Best practice:
    - rename обрабатывается ТОЛЬКО ConversationHandler
    - ConversationHandler имеет более высокий приоритет (group=0)
    - общий CallbackQueryHandler не матчится на rename
    """

    rename_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                cb_rename_entry,
                pattern=r"^dlg:rename:\d+$",
            )
        ],
        states={
            STATE_RENAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    cb_rename_receive,
                )
            ]
        },
        fallbacks=[
            CallbackQueryHandler(
                cb_rename_cancel,
                pattern=r"^dlg:cancel$",
            )
        ],
        name="dialogs-rename",
        persistent=False,
    )

    # 1️⃣ ConversationHandler — ВЫСШИЙ приоритет
    app.add_handler(rename_conv, group=0)

    # 2️⃣ Общие dialog callbacks (БЕЗ rename)
    app.add_handler(
        CallbackQueryHandler(
            cb_dialogs,
            pattern=r"^dlg:(open|delete|delete_ok|new|page|refresh|cancel|close|noop):?",
        ),
        group=1,
    )
