from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
)

from sqlalchemy import select, func
from bot.settings import load_settings
from bot.db.session import get_session
from bot.db import models as M
from bot.openai_helper import chat, transcribe_audio, generate_image
from bot.knowledge_base.indexer import sync_kb
from bot.knowledge_base.retriever import retrieve_context

_settings = load_settings()
log = logging.getLogger(__name__)

def _user_is_admin(user_id: int) -> bool:
    try:
        ids = [int(x.strip()) for x in (_settings.admin_user_ids or "").split(",") if x.strip()]
        return user_id in ids
    except:
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_session() as s:
        dbu = s.execute(select(M.User).where(M.User.tg_user_id == user.id)).scalar_one_or_none()
        if dbu is None:
            dbu = M.User(tg_user_id=user.id, is_admin=_user_is_admin(user.id), is_allowed=True, lang="ru")
            s.add(dbu); s.commit()
        dlg = s.execute(select(M.Dialog).where(M.Dialog.user_id == dbu.id, M.Dialog.is_deleted == False).order_by(M.Dialog.created_at.desc())).scalar_one_or_none()
        if dlg is None:
            title = datetime.now().strftime("%Y-%m-%d") + " | новый диалог"
            dlg = M.Dialog(user_id=dbu.id, title=title, style="expert", model=_settings.openai_model)
            s.add(dlg); s.commit()
    await update.message.reply_text("Привет! Я помогу искать ответы в документах из Базы знаний. Откройте /kb или задайте вопрос. /help — список команд.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start /help /reset /stats\n"
        "/dialogs, /dialog <id>\n"
        "/kb, /kb_diag\n"
        "/model, /mode\n"
        "/img <prompt>\n"
        "/web <query>\n"
        "/whoami, /grant <id>, /revoke <id>"
    )

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_session() as s:
        dbu = s.execute(select(M.User).where(M.User.tg_user_id == user.id)).scalar_one_or_none()
        if not dbu:
            await update.message.reply_text("Не зарегистрирован")
            return
        await update.message.reply_text(f"Ваш id={user.id}\nadmin={dbu.is_admin}\nallowed={dbu.is_allowed}")

async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _user_is_admin(update.effective_user.id):
        await update.message.reply_text("Доступ ограничён.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /grant <tg_id>")
        return
    tg_id = int(context.args[0])
    with get_session() as s:
        u = s.execute(select(M.User).where(M.User.tg_user_id == tg_id)).scalar_one_or_none()
        if not u:
            u = M.User(tg_user_id=tg_id, is_admin=False, is_allowed=True, lang="ru")
            s.add(u)
        else:
            u.is_allowed = True
        s.commit()
    await update.message.reply_text(f"Пользователь {tg_id} добавлен/разрешён.")

async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _user_is_admin(update.effective_user.id):
        await update.message.reply_text("Доступ ограничён.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /revoke <tg_id>")
        return
    tg_id = int(context.args[0])
    with get_session() as s:
        u = s.execute(select(M.User).where(M.User.tg_user_id == tg_id)).scalar_one_or_none()
        if u:
            u.is_allowed = False
            s.commit()
    await update.message.reply_text(f"Пользователь {tg_id} запрещён.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_session() as s:
        dialogs = s.execute(select(func.count(M.Dialog.id))).scalar() or 0
        messages = s.execute(select(func.count(M.Message.id))).scalar() or 0
        docs = s.execute(select(func.count(M.KbDocument.id))).scalar() or 0
    await update.message.reply_text(f"Диалогов: {dialogs}\nСообщений: {messages}\nДокументов в БЗ: {docs}")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Контекст текущего диалога очищен (псевдо).")

async def kb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    btns = [[InlineKeyboardButton("📚 Обновить БЗ (синхронизировать)", callback_data="kb:sync")]] if _user_is_admin(update.effective_user.id) else []
    btns.append([InlineKeyboardButton("🗂 Статус БЗ", callback_data="kb:status")])
    await update.message.reply_text("Меню БЗ:", reply_markup=InlineKeyboardMarkup(btns))

async def kb_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _user_is_admin(update.effective_user.id):
        await update.message.reply_text("Доступ ограничён.")
        return
    with get_session() as s:
        docs = s.execute(select(func.count(M.KbDocument.id))).scalar() or 0
        chunks = s.execute(select(func.count(M.KbChunk.id))).scalar() or 0
    await update.message.reply_text(f"Документов: {docs}\nЧанков: {chunks}")

async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if data == "kb:sync":
        if not _user_is_admin(update.effective_user.id):
            await q.edit_message_text("Доступ ограничён.")
            return
        with get_session() as s:
            res = sync_kb(s)
        await q.edit_message_text(f"Синхронизация завершена: {res}")
        return
    if data == "kb:status":
        with get_session() as s:
            docs = s.execute(select(func.count(M.KbDocument.id))).scalar() or 0
            chunks = s.execute(select(func.count(M.KbChunk.id))).scalar() or 0
        await q.edit_message_text(f"Документов: {docs}\nЧанков: {chunks}")
        return

async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = update.message.voice or update.message.audio or update.message.video_note
    if not v:
        return
    f = await context.bot.get_file(v.file_id)
    fp = f"/tmp/{v.file_unique_id}.ogg"
    await f.download_to_drive(custom_path=fp)
    text = await transcribe_audio(fp)
    await update.message.reply_text(f"Распознано: {text}")
    await handle_text_message(update, context, override_text=text)

async def img_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await update.message.reply_text("Использование: /img <описание>")
        return
    img_bytes = await generate_image(prompt)
    await update.message.reply_photo(photo=img_bytes, caption="Готово.")

async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Текущая модель: {_settings.openai_model} (список динамический не реализован в демо)")

async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Режимы: ceo | expert | pro | user (переключение для диалога — TODO)")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE, override_text: Optional[str] = None):
    user = update.effective_user
    text = override_text or (update.message and update.message.text) or ""
    if not text:
        return
    with get_session() as s:
        dbu = s.execute(select(M.User).where(M.User.tg_user_id == user.id)).scalar_one_or_none()
        if not dbu:
            await update.message.reply_text("Доступ ограничён. Обратитесь к администратору.")
            return
        if not dbu.is_allowed and not dbu.is_admin:
            await update.message.reply_text("Доступ ограничён. Обратитесь к администратору.")
            return
        dlg = s.execute(select(M.Dialog).where(M.Dialog.user_id == dbu.id, M.Dialog.is_deleted == False).order_by(M.Dialog.created_at.desc())).scalar_one_or_none()
        if not dlg:
            dlg = M.Dialog(user_id=dbu.id, title=datetime.now().strftime("%Y-%m-%d | диалог"), style="expert", model=_settings.openai_model)
            s.add(dlg); s.commit()

        try:
            ctx_rows = await retrieve_context(s, dlg.id, text, _settings.kb_top_k)
        except Exception:
            ctx_rows = []

        context_block = ""
        cites = []
        for r in ctx_rows:
            snippet = r["content"][:300].replace("\n", " ")
            context_block += f"- {snippet}\n"
            cites.append(f"{r['path']}")

        sys_prompt = "Ты — аккуратный ассистент. Если используется БЗ, обязательно ссылайся на источники."
        msgs = [{"role": "system", "content": sys_prompt}]
        if context_block:
            msgs.append({"role": "system", "content": "Контекст из БЗ:\n" + context_block})
        msgs.append({"role": "user", "content": text})

        try:
            answer = await chat(msgs, model=dlg.model or _settings.openai_model, max_tokens=800)
        except Exception:
            await update.message.reply_text("⚠ Что‑то пошло не так. Попробуйте ещё раз позже.")
            return

    if cites:
        answer += "\n\nИсточники: " + "; ".join(sorted(set(cites))[:5])
    await update.message.reply_text(answer)

def build_app() -> Application:
    app = Application.builder().token(_settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("kb", kb))
    app.add_handler(CommandHandler("kb_diag", kb_diag))
    app.add_handler(CommandHandler("img", img_cmd))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, voice_message))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))
    return app
