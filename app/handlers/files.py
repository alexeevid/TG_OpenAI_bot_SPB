# app/handlers/files.py
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..services.authz_service import AuthzService
from ..services.document_service import DocumentService
from .text import process_text

log = logging.getLogger(__name__)


def _tmp_path(unique_id: str, suffix: str) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""
    return f"/tmp/{unique_id}{suffix}"


def _default_instruction_neutral() -> str:
    return (
        "Проанализируй содержимое. Коротко перечисли, что в документе/на фото, "
        "и дай заключение о качестве (что хорошо/что не хватает). "
        "Если нужен контекст — задай 1–3 уточняющих вопроса."
    )


async def _run_extraction_and_process(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    instruction: str,
    extracted_text: str,
    origin: str,
) -> None:
    user_text = (
        f"{instruction}\n\n"
        f"---\n"
        f"ИСТОЧНИК: {origin}\n"
        f"{extracted_text}\n"
        f"---\n"
    )
    await process_text(update, context, user_text)


# ---------- Media group (3.3) ----------
async def _process_media_group(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    data = job.data or {}
    chat_id = data.get("chat_id")
    user_id = data.get("user_id")
    media_group_id = data.get("media_group_id")

    key = f"mg:{chat_id}:{user_id}:{media_group_id}"
    bucket: Dict = context.application.bot_data.get(key) or {}
    photos: List[Dict] = bucket.get("photos") or []
    caption = (bucket.get("caption") or "").strip()
    update: Update = bucket.get("update")  # stored update reference for process_text

    # cleanup early
    try:
        context.application.bot_data.pop(key, None)
    except Exception:
        pass

    if not update or not photos:
        return

    msg = update.effective_message
    if msg:
        try:
            await msg.reply_text(f"📷 Обрабатываю альбом: страниц {len(photos)}…")
        except Exception:
            pass

    svc: DocumentService | None = context.application.bot_data.get("svc_document")
    if not svc:
        if msg:
            await msg.reply_text("⚠️ DocumentService не инициализирован.")
        return

    texts: List[str] = []
    for i, ph in enumerate(photos, start=1):
        try:
            tg_file = await ph["photo"].get_file()
            local = _tmp_path(tg_file.file_unique_id, "jpg")
            await tg_file.download_to_drive(custom_path=local)
            res = svc.extract_text(local, filename=f"page_{i}.jpg", mime="image/jpeg")
            if res.text.strip():
                texts.append(f"## Page {i}\n{res.text.strip()}")
        except Exception as e:
            log.warning("media group page failed: %s", e)

    if not texts:
        if msg:
            await msg.reply_text("⚠️ Не удалось распознать текст в альбоме.")
        return

    instruction = caption or _default_instruction_neutral()
    await _run_extraction_and_process(
        update,
        context,
        instruction=instruction,
        extracted_text="\n\n".join(texts),
        origin=f"photo_album:{len(photos)}",
    )


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

    # 3.3: если это альбом — копим и обрабатываем пачкой
    if msg.media_group_id:
        key = f"mg:{msg.chat_id}:{update.effective_user.id}:{msg.media_group_id}"
        bucket: Dict = context.application.bot_data.get(key) or {"photos": [], "caption": "", "update": update}
        # caption обычно приходит на одном из сообщений альбома — сохраним первый непустой
        if msg.caption and not bucket.get("caption"):
            bucket["caption"] = msg.caption
        bucket["photos"].append({"photo": msg.photo[-1]})
        context.application.bot_data[key] = bucket

        # планируем обработку через 1.2 сек после последнего сообщения
        # (если job с таким именем уже есть — перезапишем)
        job_name = f"job_{key}"
        try:
            for j in context.job_queue.get_jobs_by_name(job_name):
                j.schedule_removal()
        except Exception:
            pass

        context.job_queue.run_once(
            _process_media_group,
            when=1.2,
            name=job_name,
            data={"chat_id": msg.chat_id, "user_id": update.effective_user.id, "media_group_id": msg.media_group_id},
        )
        return

    # одиночное фото
    await msg.reply_text("📷 Распознаю текст на изображении…")

    try:
        tg_file = await msg.photo[-1].get_file()
        local = _tmp_path(tg_file.file_unique_id, "jpg")
        await tg_file.download_to_drive(custom_path=local)

        caption = (msg.caption or "").strip()
        instruction = caption or _default_instruction_neutral()

        res = svc.extract_text(local, filename="photo.jpg", mime="image/jpeg")
        if not res.text.strip():
            await msg.reply_text("⚠️ Не удалось распознать текст на изображении.")
            return

        await _run_extraction_and_process(
            update,
            context,
            instruction=instruction,
            extracted_text=res.text,
            origin="photo",
        )

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
        instruction = caption or _default_instruction_neutral()

        res = svc.extract_text(local, filename=filename, mime=mime)
        if not (res.text or "").strip():
            await msg.reply_text("⚠️ Не удалось извлечь/распознать содержимое файла.")
            return

        origin = f"file:{filename}"
        if res.warnings:
            origin += f" warnings={','.join(res.warnings[:5])}"

        await _run_extraction_and_process(
            update,
            context,
            instruction=instruction,
            extracted_text=res.text,
            origin=origin,
        )

    except Exception as e:
        log.exception("on_document failed: %s", e)
        await msg.reply_text("❌ Ошибка обработки файла.")


def register(app: Application) -> None:
    # фото/доки должны срабатывать ДО обычного текста (text у вас в group=10)
    app.add_handler(MessageHandler(filters.PHOTO, on_photo), group=9)
    app.add_handler(MessageHandler(filters.Document.ALL, on_document), group=9)
