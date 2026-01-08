# app/handlers/files.py
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..services.authz_service import AuthzService
from ..services.document_service import DocumentService
from .text import process_text

log = logging.getLogger(__name__)


def _tmp_path(unique_id: str, suffix: str) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""
    return f"/tmp/{unique_id}{suffix}"


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return

    az: AuthzService | None = context.bot_data.get("svc_authz")
    if az and not az.is_allowed(update.effective_user.id):
        await msg.reply_text("⛔ Доступ запрещен.")
        return

    svc: DocumentService | None = context.bot_data.get("svc_document")
    if not svc:
        await msg.reply_text("⚠️ DocumentService не инициализирован.")
        return

    if not msg.photo:
        return

    await msg.reply_text("📷 Распознаю текст на изображении…")

    try:
        tg_file = await msg.photo[-1].get_file()
        local = _tmp_path(tg_file.file_unique_id, "jpg")
        await tg_file.download_to_drive(custom_path=local)

        caption = (msg.caption or "").strip()
        extracted = svc.extract_text(local, filename="photo.jpg", mime="image/jpeg")

        if not extracted.text:
            await msg.reply_text("⚠️ Не удалось распознать текст на изображении.")
            return

        # Если пользователь добавил подпись — считаем это инструкцией.
        # Иначе по умолчанию просим сделать разбор/проверку.
        instruction = caption or "Сделай разбор/проверку по этому материалу. Укажи ошибки и рекомендации."
        user_text = (
            f"{instruction}\n\n"
            f"---\n"
            f"ТЕКСТ ИЗ ИЗОБРАЖЕНИЯ (OCR):\n"
            f"{extracted.text}\n"
            f"---\n"
        )

        await process_text(update, context, user_text)

    except Exception as e:
        log.exception("on_photo failed: %s", e)
        await msg.reply_text("❌ Ошибка обработки изображения.")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return

    az: AuthzService | None = context.bot_data.get("svc_authz")
    if az and not az.is_allowed(update.effective_user.id):
        await msg.reply_text("⛔ Доступ запрещен.")
        return

    svc: DocumentService | None = context.bot_data.get("svc_document")
    if not svc:
        await msg.reply_text("⚠️ DocumentService не инициализирован.")
        return

    doc = getattr(msg, "document", None)
    if not doc:
        return

    filename = doc.file_name or "document"
    mime = doc.mime_type or None

    await msg.reply_text("📄 Анализирую документ…")

    try:
        tg_file = await doc.get_file()
        suffix = Path(filename).suffix or ""
        local = _tmp_path(tg_file.file_unique_id, suffix.replace(".", "") or "bin")
        await tg_file.download_to_drive(custom_path=local)

        caption = (msg.caption or "").strip()
        extracted = svc.extract_text(local, filename=filename, mime=mime)

        if not extracted.text:
            # частый кейс: скан-PDF без текста
            if extracted.info.startswith("pdf:no_text"):
                await msg.reply_text(
                    "⚠️ Похоже, PDF сканированный и в нём нет извлекаемого текста.\n"
                    "Пришли страницы как изображения (фото/скриншоты) — я распознаю OCR."
                )
                return

            await msg.reply_text("⚠️ Не удалось извлечь текст из документа.")
            return

        instruction = caption or "Сделай разбор/проверку по этому документу. Укажи ошибки и рекомендации."
        user_text = (
            f"{instruction}\n\n"
            f"---\n"
            f"ИЗВЛЕЧЁННЫЙ ТЕКСТ ИЗ ФАЙЛА: {filename}\n"
            f"{extracted.text}\n"
            f"---\n"
        )

        await process_text(update, context, user_text)

    except Exception as e:
        log.exception("on_document failed: %s", e)
        await msg.reply_text("❌ Ошибка обработки документа.")


def register(app: Application) -> None:
    # Фото обрабатываем раньше обычного text (text у вас в group=10)
    app.add_handler(MessageHandler(filters.PHOTO, on_photo), group=9)
    app.add_handler(MessageHandler(filters.Document.ALL, on_document), group=9)
