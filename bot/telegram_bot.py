from __future__ import annotations
import os, logging, io
from telegram import Update, constants, InlineKeyboardMarkup, InlineKeyboardButton, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, InlineQueryHandler, ContextTypes, filters
from bot.openai_helper import OpenAIHelper
from bot.usage_tracker import track_message
from bot.error_tracer import capture_exception
from bot.knowledge_base.context_manager import ContextManager
from bot.knowledge_base.retriever import Retriever
from bot.knowledge_base.loader import sync_yandex_disk_to_db
from bot.db.session import SessionLocal
from bot.db.models import Document
from bot.config import load_settings
settings = load_settings()
def is_admin(uid:int)->bool:
    ids=[int(x.strip()) for x in (settings.admin_ids or '').split(',') if x.strip().isdigit()]
    return uid in ids
class ChatGPTTelegramBot:
    def __init__(self, config, openai_helper:OpenAIHelper, retriever:Retriever, ctx_manager:ContextManager):
        self.config=config; self.openai=openai_helper; self.retriever=retriever; self.ctx_manager=ctx_manager
    def register_handlers(self, app:Application):
        app.add_handler(CommandHandler("start", self.help))
        app.add_handler(CommandHandler("help", self.help))
        app.add_handler(CommandHandler("reset", self.reset))
        app.add_handler(CommandHandler("kb", self.kb))
        app.add_handler(CommandHandler("kb_reset", self.kb_reset))
        app.add_handler(CommandHandler("kb_sync", self.kb_sync))
        app.add_handler(CommandHandler("list_models", self.list_models))
        app.add_handler(CommandHandler("set_model", self.set_model))
        app.add_handler(CommandHandler("image", self.image))
        app.add_handler(CallbackQueryHandler(self.kb_select, pattern=r"^kbselect:"))
        app.add_handler(InlineQueryHandler(self.inline_query))
        app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        app.add_handler(MessageHandler(filters.AUDIO|filters.VOICE, self.handle_voice))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.prompt))
        app.add_error_handler(self.err)
    async def help(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        await u.message.reply_text("/kb, /kb_sync, /kb_reset, /list_models, /set_model, /image, /reset")
    async def reset(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        self.openai.reset_chat_history(u.effective_chat.id); await u.message.reply_text("Сброшено")
    async def kb_reset(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        self.ctx_manager.reset(u.effective_chat.id); await u.message.reply_text("Контекст сброшен")
    async def kb_sync(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        if not is_admin(u.effective_user.id): await u.message.reply_text("Только админу"); return
        await u.message.reply_text("Стартую синхронизацию…")
        token=os.getenv("YANDEX_DISK_TOKEN",""); base=os.getenv("YANDEX_DISK_WEBDAV_URL","https://webdav.yandex.ru").rstrip("/"); root=os.getenv("YANDEX_ROOT_PATH","/База Знаний")
        if root.startswith("disk:"): root=root[5:]
        try:
            await sync_yandex_disk_to_db(token, base, root, self.openai.client, settings.embedding_model)
            await u.message.reply_text("✅ Синхронизация завершена")
        except Exception as e:
            capture_exception(e); await u.message.reply_text(f"Ошибка: {e}")
    async def kb(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        text=(u.message.text or ""); query=text.partition(" ")[2].strip(); chat_id=u.effective_chat.id
        if query:
            res=await self.retriever.search(query, document_ids=self.ctx_manager.get_selected_documents(chat_id))
            if not res: await u.message.reply_text("Ничего не найдено"); return
            msg="\n\n---\n\n".join(r[1][:1000] for r in res)
            await u.message.reply_text(msg[:4000]); return
        with SessionLocal() as s:
            docs=s.query(Document).order_by(Document.id).all()
        if not docs: await u.message.reply_text("Пусто. Сначала /kb_sync"); return
        selected=set(self.ctx_manager.get_selected_documents(chat_id))
        kb=[[InlineKeyboardButton(("✅" if d.id in selected else "❌")+f" {d.id} — {os.path.basename(d.path)}", callback_data=f"kbselect:{d.id}")] for d in docs[:100]]
        await u.message.reply_text("Выбери документы:", reply_markup=InlineKeyboardMarkup(kb))
    async def kb_select(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        q=u.callback_query; doc_id=int(q.data.split(":")[1]); chat_id=q.message.chat_id
        current=set(self.ctx_manager.get_selected_documents(chat_id))
        if doc_id in current: current.remove(doc_id)
        else: current.add(doc_id)
        self.ctx_manager.set_selected_documents(chat_id, sorted(current))
        with SessionLocal() as s: docs=s.query(Document).order_by(Document.id).all()
        kb=[[InlineKeyboardButton(("✅" if d.id in current else "❌")+f" {d.id} — {os.path.basename(d.path)}", callback_data=f"kbselect:{d.id}")] for d in docs[:100]]
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb)); await q.answer("Ок")
    async def list_models(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        try:
            allm=await self.openai.fetch_available_models()
            allowed=self.openai.allowed(allm)
            await u.message.reply_text("\n".join(allowed[:200]))
        except Exception as e:
            capture_exception(e); await u.message.reply_text(f"Ошибка: {e}")
    async def set_model(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        parts=(u.message.text or "").split(maxsplit=1)
        if len(parts)<2: await u.message.reply_text("Использование: /set_model <name>"); return
        model=parts[1].strip(); allm=await self.openai.fetch_available_models(); allowed=self.openai.allowed(allm)
        if model not in allowed: await u.message.reply_text("Не доступна"); return
        self.openai.user_models[u.effective_chat.id]=model; await u.message.reply_text(f"ok: {model}")
    async def image(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        parts=(u.message.text or "").split(maxsplit=1)
        if len(parts)<2: await u.message.reply_text("Использование: /image <описание>"); return
        url,_=await self.openai.generate_image(parts[1].strip()); await u.message.reply_photo(url)
    async def handle_photo(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        photo=u.message.photo[-1]; f=await photo.get_file(); bio=io.BytesIO(await f.download_as_bytearray())
        ans, usage=await self.openai.interpret_image(u.effective_chat.id, bio); await u.message.reply_text(ans[:4000])
        track_message(u.effective_chat.id, u.effective_user.id, "assistant", ans, self.openai.config["vision_model"], usage)
    async def handle_voice(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        voice=u.message.voice or u.message.audio; f=await voice.get_file(); path=await f.download_to_drive()
        text=await self.openai.transcribe(str(path)); await u.message.reply_text(text[:4000])
    async def inline_query(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        q=u.inline_query.query or ""; await u.inline_query.answer([InlineQueryResultArticle(id="1", title="Echo", input_message_content=InputTextMessageContent(f"Echo: {q}"))])
    async def prompt(self, u:Update, c:ContextTypes.DEFAULT_TYPE):
        ans, usage = await self.openai.get_chat_response(u.effective_chat.id, (u.message.text or ""))
        await u.message.reply_text(ans); track_message(u.effective_chat.id, u.effective_user.id, "assistant", ans, self.openai.config["model"], usage)
    async def err(self, u, c): capture_exception(c.error); logging.error("Err", exc_info=c.error)
