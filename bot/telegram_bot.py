
import logging
import asyncio
from functools import wraps
from textwrap import dedent
import os
import numpy as np

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from sqlalchemy import select

from bot.openai_helper import OpenAIHelper
from bot.knowledge_base.context_manager import ContextManager
from bot.knowledge_base.retriever import Retriever
from bot.knowledge_base.indexer import sync_yandex_to_db
from bot.usage_tracker import UsageTracker
from bot.db.session import SessionLocal
from bot.db.models import Document, DocumentChunk

def only_allowed(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        allowed = self.config.get("allowed_user_ids") or []
        if allowed and user_id not in allowed:
            await update.effective_message.reply_text("Доступ запрещён.")
            return
        return await func(self, update, context)
    return wrapper

class ChatGPTTelegramBot:
    def __init__(self, config: dict, openai_helper: OpenAIHelper, usage_tracker: UsageTracker,
                 retriever: Retriever, ctx_manager: ContextManager):
        self.config = config
        self.openai = openai_helper
        self.usage_tracker = usage_tracker
        self.retriever = retriever
        self.ctx_manager = ctx_manager

        self.page_size = 10
        self.pdf_passwords = {}  # filename -> password

        logging.info("ChatGPTTelegramBot initialized")

    def register_handlers(self, application):
        application.add_handler(CommandHandler("start", self.help))
        application.add_handler(CommandHandler("help", self.help))
        application.add_handler(CommandHandler("reset", self.reset))
        application.add_handler(CommandHandler("kb", self.kb))
        application.add_handler(CommandHandler("kb_reset", self.kb_reset))
        application.add_handler(CommandHandler("kb_search", self.kb_search))
        application.add_handler(CommandHandler("kb_sync", self.kb_sync))
        application.add_handler(CommandHandler("pdfpass", self.pdfpass))
        application.add_handler(CommandHandler("image", self.image))

        application.add_handler(CallbackQueryHandler(self.handle_kb_selection, pattern=r"^kbselect:"))
        application.add_handler(CallbackQueryHandler(self.handle_kb_page, pattern=r"^kbpage:"))

        application.add_handler(MessageHandler(filters.VOICE, self.voice))
        application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.handle_file))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))

        application.add_error_handler(self.error_handler)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logging.exception("Unhandled exception while handling update: %s", update, exc_info=context.error)
        try:
            if update and hasattr(update, "effective_message") and update.effective_message:
                await update.effective_message.reply_text("⚠️ Произошла ошибка. Уже чиним.")
        except Exception:
            pass

    # region Commands

    @only_allowed
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        HELP_TEXT = dedent("""        /start, /help — помощь
        /reset — сброс диалога

        /kb — показать документы из БЗ (+ выбор в контекст)
        /kb_reset — сброс выбранных документов
        /kb_search <вопрос> — спросить у БЗ (RAG)
        /kb_sync — синхронизировать БЗ (только админ)
        /pdfpass <filename.pdf> <password> — пароль к PDF

        /image <prompt> — сгенерировать изображение

        Голосовые — транскрибирую и сразу отвечу.
        Фото/документы — анализ в следующих релизах.
        """)
        await update.message.reply_text(HELP_TEXT)

    @only_allowed
    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.openai.reset_chat_history(chat_id)
        await update.message.reply_text("История диалога сброшена.")

    @only_allowed
    async def kb_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.ctx_manager.reset(chat_id)
        await update.message.reply_text("Контекст (выбранные документы) сброшен для этого чата.")

    @only_allowed
    async def kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_kb_list(update.effective_chat.id, update, page=0)

    async def _send_kb_list(self, chat_id: int, update_or_query, page: int):
        with SessionLocal() as s:
            total = s.query(Document).count()
            docs = s.execute(
                select(Document).order_by(Document.id).offset(page * self.page_size).limit(self.page_size)
            ).scalars().all()

        if not docs:
            text = "В базе знаний нет документов. Выполни /kb_sync (админ)"
            # Разруливаем корректно Update vs CallbackQuery
            if hasattr(update_or_query, "edit_message_text"):  # CallbackQuery
                await update_or_query.edit_message_text(text)
            elif hasattr(update_or_query, "message"):
                await update_or_query.message.reply_text(text)
            else:
                logging.warning("Can't send message: unknown object type in _send_kb_list")
            return

        text_lines = [f"📚 Документы ({total} всего). Страница {page+1}:"]

        kb = []
        for d in docs:
            text_lines.append(f"• [{d.id}] {d.path}")
            kb.append([InlineKeyboardButton(f"➕ {d.id}", callback_data=f"kbselect:{d.id}")])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"kbpage:{page-1}"))
        if (page + 1) * self.page_size < total:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"kbpage:{page+1}"))
        if nav:
            kb.append(nav)

        text = "\n".join(text_lines)
        markup = InlineKeyboardMarkup(kb)

        if hasattr(update_or_query, "edit_message_text"):  # CallbackQuery
            await update_or_query.edit_message_text(text, reply_markup=markup)
        elif hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(text, reply_markup=markup)
        else:
            logging.warning("Can't send message: unknown object type in _send_kb_list")

    async def handle_kb_page(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        _, page = query.data.split(":")
        page = int(page)
        await self._send_kb_list(update.effective_chat.id, query, page)

    async def handle_kb_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        _, doc_id = query.data.split(":")
        doc_id = int(doc_id)
        chat_id = update.effective_chat.id
        self.ctx_manager.add(chat_id, doc_id)
        await query.edit_message_text(f"Документ {doc_id} добавлен в контекст чата.")

    @only_allowed
    async def kb_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        question = " ".join(context.args) if context.args else ""
        if not question:
            await update.message.reply_text("Использование: /kb_search <вопрос>")
            return

        q_emb = (await self.openai.embed_texts([question]))[0]
        doc_ids = self.ctx_manager.get(chat_id)
        if not doc_ids:
            await update.message.reply_text("Сначала выбери документы командой /kb.")
            return

        with SessionLocal() as s:
            chunks = (
                s.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id.in_(doc_ids))
                )
                .scalars()
                .all()
            )

        if not chunks:
            await update.message.reply_text("В выбранных документах нет текста.")
            return

        q = np.array(q_emb, dtype=np.float32)
        q_norm = np.linalg.norm(q) + 1e-8
        scored = []
        for ch in chunks:
            v = np.array(ch.embedding, dtype=np.float32)
            score = float(np.dot(q, v) / ((np.linalg.norm(v)+1e-8) * q_norm))
            scored.append((score, ch))
        scored.sort(key=lambda x: x[0], reverse=True)
        top_chunks = [c for _, c in scored[:5]]

        context_text = "\n\n".join([c.text for c in top_chunks])
        prompt = f"Вопрос пользователя: {question}\n\nКонтекст из выбранных документов:\n{context_text}\n\nДай подробный ответ, опираясь на контекст (если релевантно)."

        answer, _ = await self.openai.get_chat_response(chat_id, prompt)
        await update.message.reply_text(answer)

    @only_allowed
    async def kb_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        admins = self.config.get("admin_user_ids") or []
        if admins and user_id not in admins:
            await update.message.reply_text("Команда доступна только администраторам.")
            return

        token = os.getenv("YANDEX_DISK_TOKEN")
        root_path = os.getenv("YANDEX_ROOT_PATH", "/")
        if not token:
            await update.message.reply_text("YANDEX_DISK_TOKEN не задан.")
            return

        await update.message.reply_text("Начал синхронизацию. Это может занять время...")
        try:
            await sync_yandex_to_db(
                yandex_token=token,
                root_path=root_path,
                embedder=self.openai.embed_texts,
                embedding_model=self.openai.config.get("embedding_model", "text-embedding-3-small"),
                pdf_passwords=self.pdf_passwords
            )
            await update.message.reply_text("Готово!")
        except Exception as e:
            logging.exception("kb_sync error")
            await update.message.reply_text(f"Ошибка: {e}")

    @only_allowed
    async def pdfpass(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) < 2:
            await update.message.reply_text("Использование: /pdfpass <имя_файла.pdf> <пароль>")
            return
        filename = context.args[0]
        password = " ".join(context.args[1:])
        self.pdf_passwords[filename] = password
        await update.message.reply_text(f"Пароль для {filename} сохранён на время жизни процесса.")

    @only_allowed
    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.config.get("enable_image_generation", True):
            await update.message.reply_text("Генерация изображений отключена.")
            return
        prompt = " ".join(context.args) if context.args else ""
        if not prompt:
            await update.message.reply_text("Укажи промпт: /image <prompt>")
            return
        try:
            url, size = await self.openai.generate_image(prompt)
            await update.message.reply_text(f"Сгенерировал ({size}): {url}")
        except Exception as e:
            await update.message.reply_text(str(e))

    @only_allowed
    async def voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        file = await context.bot.get_file(update.message.voice.file_id)
        tmp = f"/tmp/{file.file_unique_id}.oga"
        await file.download_to_drive(tmp)
        try:
            text = await self.openai.transcribe(tmp)
            answer, _ = await self.openai.get_chat_response(chat_id, text)
            await update.message.reply_text(answer)
        except Exception as e:
            await update.message.reply_text(f"Ошибка при транскрибации: {e}")

    @only_allowed
    async def handle_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Анализ файлов будет в следующем релизе.")

    @only_allowed
    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        query = update.message.text
        try:
            answer, _ = await self.openai.get_chat_response(chat_id, query)
            await update.message.reply_text(answer)
        except Exception as e:
            logging.exception("Error on text message")
            await update.message.reply_text(f"Ошибка: {e}")
