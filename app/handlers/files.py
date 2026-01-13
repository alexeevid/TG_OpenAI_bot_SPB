# app/handlers/files.py
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..services.authz_service import AuthzService
from ..services.dialog_service import DialogService
from ..services.document_service import DocumentService
from .text import process_text

log = logging.getLogger(__name__)


def _tmp_path(unique_id: str, suffix: str) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""
    return f"/tmp/{unique_id}{suffix}"


def _default_instruction_for_image() -> str:
    return (
        "Опиши, что изображено на картинке. "
        "Сделай краткий вывод или интересное наблюдение. "
        "Если уместно — предложи 1–2 варианта, что можно разобрать подробнее."
    )


def _default_instruction_for_document() -> str:
    return (
        "Проанализируй содержимое. "
        "Коротко опиши, что это за документ или таблица, "
        "и дай заключение о качестве (что хорошо и чего не хватает). "
        "Если нужен контекст — задай 1–3 уточняющих вопроса."
    )


async def _run(update: Update, context: ContextTypes.DEFAULT_TYPE, instruction: str, payload: str, origin: str) -> None:
    user_text = (
        f"{instruction}\n\n"
        f"---\n"
        f"ИСТОЧНИК: {origin}\n"
        f"{payload}\n"
        f"---\n"
    )
    await process_text(update, context, user_text)


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

    await msg.reply_text("🖼 Анализирую изображение…")

    try:
        tg_file = await msg.photo[-1].get_file()
        local = _tmp_path(tg_file.file_unique_id, "jpg")
        await tg_file.download_to_drive(custom_path=local)

        caption = (msg.caption or "").strip()
        res = svc.extract_text(local, filename="photo.jpg", mime="image/jpeg")

        # --- сохраняем контекст вложения в активный диалог (персистентно) ---
        ds: DialogService | None = context.bot_data.get("svc_dialog")
        if ds:
            try:
                asset_text = (res.text or "").strip()
                asset_desc = (res.description or "").strip()
                ds.add_dialog_asset(
                    update.effective_user.id,
                    {
                        "type": "photo",
                        "kind": res.kind,
                        "source": "telegram",
                        "filename": "photo.jpg",
                        "caption": caption,
                        "text_excerpt": asset_text[:6000],
                        "description": asset_desc[:2000],
                    },
                    keep_last=5,
                )
            except Exception:
                pass

        # --- выбор инструкции ---
        if caption:
            instruction = caption
        else:
            if res.kind == "image":
                instruction = _default_instruction_for_image()
            else:
                instruction = _default_instruction_for_document()

        # --- формирование payload ---
        if not (res.text or "").strip() and (res.description or "").strip():
            payload = (
                f"ТИП ИЗОБРАЖЕНИЯ: {res.kind}\n"
                f"ОПИСАНИЕ:\n{res.description}"
            )
            await _run(update, context, instruction, payload, origin=f"photo kind={res.kind}")
            return

        if not (res.text or "").strip():
            await msg.reply_text("⚠️ Не удалось извлечь текст или описание. Попробуй прислать изображение более чётко.")
            return

        payload = (
            f"ТИП ИЗОБРАЖЕНИЯ: {res.kind}\n"
            f"ТЕКСТ (OCR):\n{res.text}"
        )
        await _run(update, context, instruction, payload, origin=f"photo kind={res.kind}")

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

    await msg.reply_text("📄 Анализирую файл…")

    try:
        tg_file = await doc.get_file()
        suffix = Path(filename).suffix or ".bin"
        local = _tmp_path(tg_file.file_unique_id, suffix.lstrip("."))
        await tg_file.download_to_drive(custom_path=local)

        caption = (msg.caption or "").strip()
        res = svc.extract_text(local, filename=filename, mime=mime)

        # --- сохраняем контекст вложения в активный диалог (персистентно) ---
        ds: DialogService | None = context.bot_data.get("svc_dialog")
        if ds:
            try:
                asset_text = (res.text or "").strip()
                asset_desc = (res.description or "").strip()
                ds.add_dialog_asset(
                    update.effective_user.id,
                    {
                        "type": "document",
                        "kind": res.kind,
                        "source": "telegram",
                        "filename": filename,
                        "mime": mime or "",
                        "caption": caption,
                        "text_excerpt": asset_text[:8000],
                        "description": asset_desc[:2000],
                    },
                    keep_last=5,
                )
            except Exception:
                pass

        instruction = caption or _default_instruction_for_document()

        if not (res.text or "").strip() and (res.description or "").strip():
            payload = f"ТИП: {res.kind}\nОПИСАНИЕ:\n{res.description}"
            await _run(update, context, instruction, payload, origin=f"file:{filename} kind={res.kind}")
            return

        if not (res.text or "").strip():
            await msg.reply_text("⚠️ Не удалось извлечь/распознать содержимое файла.")
            return

        payload = f"ТИП: {res.kind}\nИЗВЛЕЧЁННЫЙ ТЕКСТ:\n{res.text}"
        await _run(update, context, instruction, payload, origin=f"file:{filename} kind={res.kind}")

    except Exception as e:
        log.exception("on_document failed: %s", e)
        await msg.reply_text("❌ Ошибка обработки файла.")


def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.PHOTO, on_photo), group=9)
    app.add_handler(MessageHandler(filters.Document.ALL, on_document), group=9)
