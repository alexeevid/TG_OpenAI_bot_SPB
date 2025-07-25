
import logging
from functools import wraps
from typing import Optional, List

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackContext,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from bot.config import load_settings, Settings
from bot.openai_helper import OpenAIHelper
from bot.db.session import init_db
from bot.db.models import Base, Document
from bot.db.session import SessionLocal
from bot.knowledge_base.indexer import sync_disk_to_db
from bot.knowledge_base.yandex_rest import YandexDiskREST
from sqlalchemy import select

settings = load_settings()

def only_allowed(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if settings.allowed_set and user_id not in settings.allowed_set and user_id not in settings.admin_set:
            await update.message.reply_text("Доступ к боту ограничен. Обратитесь к администратору.")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapper

def only_admin(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id not in settings.admin_set:
            await update.message.reply_text("Команда доступна только администраторам.")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapper

class ChatGPTTelegramBot:
    def __init__(self, openai_helper: OpenAIHelper):
        self.openai = openai_helper
        self.selected_docs: dict[int, List[str]] = {}

    def register(self, app: Application):
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.help))
        app.add_handler(CommandHandler("reset", self.reset))
        app.add_handler(CommandHandler("kb", self.kb))
        app.add_handler(CommandHandler("kb_search", self.kb_search))
        app.add_handler(CommandHandler("kb_reset", self.kb_reset))
        app.add_handler(CommandHandler("kb_sync", self.kb_sync))
        app.add_handler(CommandHandler("image", self.image_cmd))
        app.add_handler(CallbackQueryHandler(self.kb_select, pattern=r"^kbselect:"))

        # text messages
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

    async def post_init(self, app: Application):
        commands = [
            BotCommand("start", "помощь"),
            BotCommand("help", "помощь"),
            BotCommand("reset", "сброс диалога"),
            BotCommand("kb", "база знаний (выбор документов)"),
            BotCommand("kb_search", "поиск по выбранным документам"),
            BotCommand("kb_reset", "сброс выбранных документов"),
            BotCommand("kb_sync", "синхронизировать Я.Диск в БД (админ)"),
            BotCommand("image", "сгенерировать изображение"),
        ]
        await app.bot.set_my_commands(commands)

    # ---- Commands ----

    @only_allowed
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        return await self.help(update, context)

    def post_init(self, app: Application):
        logging.info("Post-init hook called. Nothing to initialize yet.")
    
    @only_allowed
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        txt = ("""Я умею:
- Отвечать на вопросы (/reset чтобы очистить контекст)
- Работать с базой знаний через Яндекс.Диск:
/kb — показать файлы
/kb_search <вопрос> — поиск по выбранным файлам
/kb_reset — сброс выбора
/kb_sync — синхронизация (только админ)
/image <prompt> — сгенерировать изображение
""")
        await update.message.reply_text(txt)

    @only_allowed
    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Контекст очищен (фейк, т.к. мы не копим историю в этой версии).")

    @only_allowed
    async def kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        with SessionLocal() as s:
            docs = list(s.scalars(select(Document)).all())
        if not docs:
            await update.message.reply_text("Нет документов в БД. Запустите /kb_sync (админ)")
            return
        keyboard = []
        for d in docs[:30]:
            keyboard.append([InlineKeyboardButton(text=d.path, callback_data=f"kbselect:{chat_id}:{d.path}")])
        await update.message.reply_text("Выберите документы (жмите по кнопкам)", reply_markup=InlineKeyboardMarkup(keyboard))

    async def kb_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        _, chat_id_str, path = query.data.split(":", 2)
        chat_id = int(chat_id_str)
        self.selected_docs.setdefault(chat_id, [])
        if path not in self.selected_docs[chat_id]:
            self.selected_docs[chat_id].append(path)
        await query.edit_message_text(f"Добавлен документ: {path}\nТекущий контекст ({len(self.selected_docs[chat_id])}):\n" + "\n".join(self.selected_docs[chat_id]))

    @only_allowed
    async def kb_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.selected_docs.pop(chat_id, None)
        await update.message.reply_text("Контекст очищен.")

    @only_admin
    async def kb_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            added = sync_disk_to_db(settings.yandex_disk_token, settings.yandex_root_path)
            await update.message.reply_text(f"Синхронизация завершена. Добавлено: {added}")
        except Exception as e:
            logging.exception("kb_sync error")
            await update.message.reply_text(f"Ошибка: {e}")

    @only_allowed
    async def kb_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not self.selected_docs.get(chat_id):
            await update.message.reply_text("Сначала выберите документы командой /kb")
            return
        question = " ".join(context.args) if context.args else None
        if not question:
            await update.message.reply_text("Использование: /kb_search <вопрос>")
            return
        # Упрощенная версия: просто сообщаем, что контекст учтен.
        prompt = f"Вопрос: {question}\nУчитывай эти документы: {self.selected_docs[chat_id]}\nОтветь максимально полезно."
        ans = self.openai.chat([
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt}
        ])
        await update.message.reply_text(ans)

    @only_allowed
    async def image_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        prompt = " ".join(context.args) if context.args else None
        if not prompt:
            await update.message.reply_text("Использование: /image <prompt>")
            return
        try:
            url = self.openai.generate_image(prompt)
            await update.message.reply_photo(photo=url, caption=prompt)
        except Exception as e:
            await update.message.reply_text(f"Ошибка генерации изображения: {e}")

    @only_allowed
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.message.text
        ans = self.openai.chat([
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": q}
        ])
        await update.message.reply_text(ans)
