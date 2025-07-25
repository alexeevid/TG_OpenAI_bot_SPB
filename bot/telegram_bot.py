
import logging
import asyncio
from functools import wraps
from textwrap import dedent

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

from bot.openai_helper import OpenAIHelper
from bot.knowledge_base.context_manager import ContextManager
from bot.knowledge_base.retriever import Retriever
from bot.usage_tracker import UsageTracker

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

        self.awaiting_pdf_password = {}

        # stats
        logging.info("ChatGPTTelegramBot initialized")

    def register_handlers(self, application: Application):
        application.add_handler(CommandHandler("start", self.help))
        application.add_handler(CommandHandler("help", self.help))
        application.add_handler(CommandHandler("reset", self.reset))
        application.add_handler(CommandHandler("kb", self.kb))
        application.add_handler(CommandHandler("kb_reset", self.kb_reset))
        application.add_handler(CommandHandler("pdfpass", self.pdfpass))
        application.add_handler(CommandHandler("image", self.image))
        application.add_handler(MessageHandler(filters.VOICE, self.voice))
        application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.handle_file))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))

    # ------------- commands -------------

    @only_allowed
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        HELP_TEXT = dedent("""\
        /start, /help — помощь
        /reset — сброс диалога

        /kb [запрос] — показать файлы/поиск в БЗ
        /kb_reset — сброс выбранного контекста
        /pdfpass <file> <password> — пароль к PDF
        /image <prompt> — сгенерировать изображение

        Также:
        • Пришли голос — я транскрибирую и СРАЗУ отвечу по смыслу.
        • Пришли фото/документ — проанализирую и отвечу.
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
        await update.message.reply_text("База знаний пока подключена только на уровне кода без UI выбора. Скоро допишем.")

    @only_allowed
    async def pdfpass(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Поддержка паролей к PDF в этом минимальном билде отключена.")

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
            # и сразу ответить, добавив запрос в историю
            answer, _ = await self.openai.get_chat_response(chat_id, text)
            await update.message.reply_text(answer)
        except Exception as e:
            await update.message.reply_text(f"Ошибка при транскрибации: {e}")

    @only_allowed
    async def handle_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Анализ файлов в этом минимальном билде ещё не закончен.")

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
