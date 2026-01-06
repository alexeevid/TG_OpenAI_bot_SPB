from typing import List, Literal, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from ..services.dialog_service import DialogService

BTN_KIND_PREFIX = "model:kind:"
BTN_SET_PREFIX = "model:set:"


ModelKind = Literal["text", "image", "transcribe"]

KIND_LABELS: Dict[ModelKind, str] = {
    "text": "Текст",
    "image": "Изображения",
    "transcribe": "Распознавание речи",
}


def _get_openai(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get("openai")


def _get_dialog_service(context: ContextTypes.DEFAULT_TYPE) -> DialogService | None:
    ds = context.bot_data.get("dialog_service")
    return ds if isinstance(ds, DialogService) else ds  # мягко, чтобы не ломать DI


def _get_available_models(context: ContextTypes.DEFAULT_TYPE, kind: ModelKind) -> List[str]:
    """
    Возвращает список доступных моделей по модальности
    напрямую из OpenAI API (через OpenAIClient).
    """
    openai = _get_openai(context)
    if not openai:
        return []

    try:
        return list(openai.list_models_by_kind(kind))
    except Exception:
        return []


def _format_current_models(models: Dict[str, str]) -> str:
    return (
        "Текущие модели активного диалога:\n"
        f"• Текст: `{models.get('text_model', 'unknown')}`\n"
        f"• Изображения: `{models.get('image_model', 'unknown')}`\n"
        f"• Распознавание: `{models.get('transcribe_model', 'unknown')}`"
    )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return

    dialog_service = _get_dialog_service(context)
    if not dialog_service:
        await update.effective_message.reply_text("Ошибка: DialogService не настроен.")
        return

    current = dialog_service.get_active_models(update.effective_user.id)

    kb = [
        [InlineKeyboardButton(f"{KIND_LABELS['text']} (сейчас: {current['text_model']})", callback_data=f"{BTN_KIND_PREFIX}text")],
        [InlineKeyboardButton(f"{KIND_LABELS['image']} (сейчас: {current['image_model']})", callback_data=f"{BTN_KIND_PREFIX}image")],
        [InlineKeyboardButton(f"{KIND_LABELS['transcribe']} (сейчас: {current['transcribe_model']})", callback_data=f"{BTN_KIND_PREFIX}transcribe")],
    ]

    await update.effective_message.reply_text(
        _format_current_models(current) + "\n\nВыберите, какую модальность настраиваем:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


async def on_kind_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return

    if not query.data.startswith(BTN_KIND_PREFIX):
        return

    kind_s = query.data[len(BTN_KIND_PREFIX) :]
    if kind_s not in ("text", "image", "transcribe"):
        await query.answer("Неизвестная модальность", show_alert=True)
        return

    kind: ModelKind = kind_s  # type: ignore

    dialog_service = _get_dialog_service(context)
    if not dialog_service:
        await query.answer("DialogService не настроен", show_alert=True)
        return

    current = dialog_service.get_active_models(query.from_user.id)
    current_model_key = f"{kind}_model"
    current_model = current.get(current_model_key, "unknown")

    models = _get_available_models(context, kind)

    if not models:
        await query.answer()
        await query.edit_message_text(
            _format_current_models(current)
            + f"\n\n⚠️ Сейчас не удалось получить список моделей для «{KIND_LABELS[kind]}».",
            parse_mode="Markdown",
        )
        return

    kb = []
    for m in models:
        label = f"✅ {m}" if m == current_model else m
        kb.append([InlineKeyboardButton(label, callback_data=f"{BTN_SET_PREFIX}{kind}:{m}")])

    # Кнопка "назад" к выбору модальности
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"{BTN_KIND_PREFIX}__back")])

    await query.answer()
    await query.edit_message_text(
        _format_current_models(current)
        + f"\n\nВыберите модель для «{KIND_LABELS[kind]}»:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


async def on_set_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return

    if not query.data.startswith(BTN_SET_PREFIX):
        return

    payload = query.data[len(BTN_SET_PREFIX) :]
    if ":" not in payload:
        await query.answer("Некорректные данные", show_alert=True)
        return

    kind_s, model = payload.split(":", 1)
    if kind_s not in ("text", "image", "transcribe"):
        await query.answer("Неизвестная модальность", show_alert=True)
        return

    kind: ModelKind = kind_s  # type: ignore

    dialog_service = _get_dialog_service(context)
    if not dialog_service:
        await query.answer("DialogService не настроен", show_alert=True)
        return

    # Сохраняем в активный диалог (в БД)
    dialog_service.set_active_model(query.from_user.id, kind, model)

    # Показываем обновлённые текущие модели
    current = dialog_service.get_active_models(query.from_user.id)

    await query.answer(f"Выбрана модель для «{KIND_LABELS[kind]}»: {model}", show_alert=False)
    await query.edit_message_text(
        _format_current_models(current) + "\n\n✅ Изменение сохранено.",
        parse_mode="Markdown",
    )


async def on_kind_back_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработка "назад" из списка моделей — возвращаемся к выбору модальности.
    """
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return

    if query.data != f"{BTN_KIND_PREFIX}__back":
        return

    dialog_service = _get_dialog_service(context)
    if not dialog_service:
        await query.answer("DialogService не настроен", show_alert=True)
        return

    current = dialog_service.get_active_models(query.from_user.id)

    kb = [
        [InlineKeyboardButton(f"{KIND_LABELS['text']} (сейчас: {current['text_model']})", callback_data=f"{BTN_KIND_PREFIX}text")],
        [InlineKeyboardButton(f"{KIND_LABELS['image']} (сейчас: {current['image_model']})", callback_data=f"{BTN_KIND_PREFIX}image")],
        [InlineKeyboardButton(f"{KIND_LABELS['transcribe']} (сейчас: {current['transcribe_model']})", callback_data=f"{BTN_KIND_PREFIX}transcribe")],
    ]

    await query.answer()
    await query.edit_message_text(
        _format_current_models(current) + "\n\nВыберите, какую модальность настраиваем:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


def register(app) -> None:
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CallbackQueryHandler(on_kind_back_cb, pattern=r"^model:kind:__back$"))
    app.add_handler(CallbackQueryHandler(on_kind_cb, pattern=r"^model:kind:"))
    app.add_handler(CallbackQueryHandler(on_set_cb, pattern=r"^model:set:"))
