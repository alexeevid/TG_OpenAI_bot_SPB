import logging
from typing import List
from functools import wraps

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from .openai_helper import OpenAIHelper
from .knowledge_base.context_manager import ContextManager
from .knowledge_base.yandex_rest import YandexDiskREST
from .knowledge_base.indexer import sync_yandex_to_db
from .db.session import SessionLocal, engine
from .db.models import Document

def only_allowed(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id
        if self.allowed and uid not in self.allowed:
            await update.message.reply_text("⛔️ Доступ запрещен.")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapper

def only_admin(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id
        if uid not in self.admins:
            await update.message.reply_text("Команда доступна только администраторам.")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapper

class ChatGPTTelegramBot:
    def __init__(self, token: str, openai_helper: OpenAIHelper, yandex_token: str, yandex_root: str,
                 admins: List[int], allowed: List[int] | None):
        self.token = token
        self.openai = openai_helper
        self.yd_token = yandex_token
        self.yd_root = yandex_root
        self.admins = admins or []
        self.allowed = allowed or []
        self.ctx_manager = ContextManager()

    def register(self, app: Application):
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.help))
        app.add_handler(CommandHandler("reset", self.reset))
        app.add_handler(CommandHandler("kb", self.kb))
        app.add_handler(CommandHandler("kb_reset", self.kb_reset))
        app.add_handler(CommandHandler("kb_search", self.kb_search))
        app.add_handler(CommandHandler("kb_sync", self.kb_sync))
        app.add_handler(CommandHandler("image", self.image))
        app.add_handler(CallbackQueryHandler(self.kb_select, pattern=r"^kb:"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Привет! /help — список команд.")

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
"""/reset — сброс диалога
/kb — выбрать документы из базы знаний
/kb_search <вопрос> — задать вопрос по выбранным документам
/kb_reset — сбросить выбранные документы
/kb_sync — синхронизировать БЗ (админ)
/image <prompt> — сгенерировать изображение"""
        )

    @only_allowed
    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.openai.reset_chat_history(chat_id)
        self.ctx_manager.reset(chat_id)
        await update.message.reply_text("История очищена.")

    @only_allowed
    async def kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if engine is None:
            await update.message.reply_text("DB не настроена, /kb_sync недоступен.")
            return
        session = SessionLocal()
        try:
            docs = session.query(Document).order_by(Document.path).all()
        finally:
            session.close()
        if not docs:
            await update.message.reply_text("В Базе знаний ничего нет. Сначала /kb_sync (админ).")
            return
        # Пагинация по 20
        page = 0
        await self._send_kb_page(update, docs, page)

    async def _send_kb_page(self, update_or_query, docs, page: int):
        per_page = 20
        start = page * per_page
        chunk = docs[start:start+per_page]
        buttons = []
        for d in chunk:
            buttons.append([InlineKeyboardButton(d.path, callback_data=f"kb:toggle:{d.path}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("« Назад", callback_data=f"kb:page:{page-1}"))
        if start + per_page < len(docs):
            nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"kb:page:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("✅ Применить", callback_data="kb:apply")])
        text = f"Выберите документы (страница {page+1}/{(len(docs)+per_page-1)//per_page})"
        if hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    async def kb_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        chat_id = query.message.chat_id
        if not hasattr(self, "_kb_selection"):
            self._kb_selection = {}
        selected = self._kb_selection.setdefault(chat_id, set())
        if data.startswith("kb:page:"):
            page = int(data.split(":")[2])
            session = SessionLocal()
            try:
                docs = session.query(Document).order_by(Document.path).all()
            finally:
                session.close()
            await self._send_kb_page(query, docs, page)
            return
        if data.startswith("kb:toggle:"):
            path = data[len("kb:toggle:"):]
            if path in selected:
                selected.remove(path)
            else:
                selected.add(path)
            await query.answer("Готово")
            return
        if data == "kb:apply":
            self.ctx_manager.set_docs(chat_id, list(selected))
            await query.edit_message_text(f"Выбрано {len(selected)} документов. Можно задавать вопросы через /kb_search <вопрос>.")
            return

    @only_allowed
    async def kb_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.ctx_manager.reset(chat_id)
        await update.message.reply_text("Контекст БЗ сброшен.")

    @only_allowed
    async def kb_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        question = " ".join(context.args) if context.args else None
        if not question:
            await update.message.reply_text("Использование: /kb_search <вопрос>")
            return
        docs = self.ctx_manager.get_docs(chat_id)
        if not docs:
            await update.message.reply_text("Сначала выбери документы через /kb")
            return
        # На текущем этапе просто перечислим выбранные файлы и дернем OpenAI без реального RAG
        context_text = "\n".join(f"[{i+1}] {p}" for i,p in enumerate(docs[:10]))
        prompt = f"""Ответь на вопрос пользователя, учитывая, что ему интересны документы:
{context_text}

Вопрос: {question}
"""
        answer, _ = await self.openai.get_chat_response(chat_id, prompt)
        await update.message.reply_text(answer)

    @only_admin
    async def kb_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            sync_yandex_to_db(self.yd_token, self.yd_root)
            await update.message.reply_text("Синхронизация завершена.")
        except Exception as e:
            logging.exception("kb_sync error")
            await update.message.reply_text(f"Ошибка: {e}")

    @only_allowed
    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        prompt = " ".join(context.args) if context.args else None
        if not prompt:
            await update.message.reply_text("Использование: /image <prompt>")
            return
        try:
            url, _ = await self.openai.generate_image(prompt)
            await update.message.reply_text(url)
        except Exception as e:
            await update.message.reply_text(f"Ошибка генерации: {e}")

    @only_allowed
    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        text = update.message.text
        answer, _ = await self.openai.get_chat_response(chat_id, text)
        await update.message.reply_text(answer)
