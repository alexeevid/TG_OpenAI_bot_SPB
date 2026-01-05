from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

BTN_MODEL_PREFIX = "model:set:"


def _get_available_models(cfg) -> List[str]:
    """
    Возвращает список доступных моделей.
    Сейчас — минимально безопасно:
    либо из denylist/allowlist,
    либо только текущая модель.
    """
    # Если в будущем появится список — сюда легко добавить
    model = getattr(cfg, "text_model", None)
    if model:
        return [model]
    return []


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data.get("settings")
    if not cfg or not update.effective_message:
        return

    current_model = getattr(cfg, "text_model", "unknown")
    models = _get_available_models(cfg)

    # Если моделей нет — всегда отвечаем текстом
    if not models:
        await update.effective_message.reply_text(
            f"Текущая модель: `{current_model}`\n\n"
            "Другие модели сейчас недоступны.",
            parse_mode="Markdown",
        )
        return

    # Строим клавиатуру
    kb = []
    for m in models:
        label = f"✅ {m}" if m == current_model else m
        kb.append([InlineKeyboardButton(label, callback_data=f"{BTN_MODEL_PREFIX}{m}")])

    markup = InlineKeyboardMarkup(kb)

    await update.effective_message.reply_text(
        f"Текущая модель:\n`{current_model}`\n\nВыберите модель:",
        reply_markup=markup,
        parse_mode="Markdown",
    )


async def on_model_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    if not query.data.startswith(BTN_MODEL_PREFIX):
        return

    cfg = context.bot_data.get("settings")
    if not cfg:
        await query.answer("Ошибка конфигурации", show_alert=True)
        return

    model = query.data[len(BTN_MODEL_PREFIX) :]

    # Сейчас безопасно: просто подтверждаем выбор
    # (реальная смена модели может быть добавлена позже)
    await query.answer(f"Выбрана модель: {model}", show_alert=False)

    await query.message.edit_text(
        f"Текущая модель:\n`{model}`",
        parse_mode="Markdown",
    )


def register(app) -> None:
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CallbackQueryHandler(on_model_cb, pattern=r"^model:set:"))
