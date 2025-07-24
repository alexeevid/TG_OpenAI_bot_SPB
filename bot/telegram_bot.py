from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Optional

from telegram import (Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from bot.openai_helper import OpenAIHelper
from bot.usage_tracker import UsageTracker
from bot.error_tracer import capture_exception
from bot.config import load_settings
from bot.db.session import SessionLocal
from bot.db.models import Document
from bot.knowledge_base.context_manager import ContextManager
from bot.knowledge_base.retriever import Retriever
from bot.knowledge_base.loader import sync_yandex_disk_to_db

from bot.knowledge_base.passwords import (
    set_awaiting_password,
    get_awaiting_password_file,
    clear_awaiting_password,
    store_pdf_password,
)

settings = load_settings()
ALLOWED_USER_IDS = set(int(x.strip()) for x in (settings.allowed_user_ids or "").split(",") if x.strip().isdigit())
ADMIN_IDS = set(int(x.strip()) for x in (settings.admin_ids or "").split(",") if x.strip().isdigit())

def only_allowed(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
            await update.effective_message.reply_text("⛔ Доступ к боту ограничен. Обратись к администратору.")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapper

def only_admin(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in ADMIN_IDS:
            await update.effective_message.reply_text("⛔ Только администраторы могут выполнять эту команду.")
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapper

class ChatGPTTelegramBot:
    def __init__(self, config: dict, openai_helper: OpenAIHelper, usage_tracker: Optional[UsageTracker] = None,
                 retriever: Optional[Retriever] = None, ctx_manager: Optional[ContextManager] = None):
        self.config = config
        self.openai = openai_helper
        self.usage_tracker = usage_tracker or UsageTracker()
        self.retriever = retriever or Retriever()
        self.ctx_manager = ctx_manager or ContextManager()

    def register_handlers(self, application: Application):
        application.add_handler(CommandHandler("start", self.help))
        application.add_handler(CommandHandler("help", self.help))
        application.add_handler(CommandHandler("reset", self.reset))

        application.add_handler(CommandHandler("kb", self.show_knowledge_base))
        application.add_handler(CommandHandler("kb_reset", self.kb_reset))
        application.add_handler(CommandHandler("kb_sync", self.kb_sync))
        application.add_handler(CommandHandler("pdfpass", self.pdf_pass_command))

        application.add_handler(CommandHandler("list_models", self.list_models))
        application.add_handler(CommandHandler("set_model", self.set_model))
        application.add_handler(CallbackQueryHandler(self.handle_model_selection, pattern=r"^setmodel:"))
        application.add_handler(CallbackQueryHandler(self.handle_kb_selection, pattern=r"^kbselect:"))

        if self.config.get("enable_image_generation", False):
            application.add_handler(CommandHandler("image", self.image))

        application.add_handler(MessageHandler(filters.Document.ALL, self.handle_file_upload))
        application.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self.handle_voice))

        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_password_input))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.prompt))

        application.add_error_handler(self.global_error_handler)

    @only_allowed
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(HELP_TEXT)
            from textwrap import dedent
            
            HELP_TEXT = dedent("""\
            /start, /help — помощь
            /reset — сброс диалога
            /kb [запрос] — показать файлы/поиск в БЗ
            /kb_reset — сброс выбранного контекста
            /kb_sync — синхронизация базы знаний (админ)
            /pdfpass <file.pdf> <password> — пароль к PDF
            /list_models — показать доступные модели (кнопки)
            /set_model <name> — выбрать модель вручную
            /image <prompt> — сгенерировать изображение
            
            Также:
            — Пришли голос — я транскрибирую и СРАЗУ отвечу по смыслу.
            — Пришли фото/документ — проанализирую и отвечу.
            """)

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

    @only_admin
    async def kb_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        token_raw = os.getenv("YANDEX_DISK_TOKEN", "").strip()
        token = token_raw.split(None, 1)[1].strip() if token_raw.lower().startswith("oauth ") else token_raw
        base_url = os.getenv("YANDEX_DISK_WEBDAV_URL", "https://webdav.yandex.ru").rstrip("/")
        root_path = os.getenv("YANDEX_ROOT_PATH", "/База Знаний")

        await update.message.reply_text("Стартую синхронизацию…")
        try:
            await sync_yandex_disk_to_db(
                token=token, base_url=base_url, root_path=root_path,
                embedding_client=None, embedding_model=settings.embedding_model
            )
            await update.message.reply_text("✅ Синхронизация завершена")
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка при синхронизации: {e}")

    @only_allowed
    async def pdf_pass_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("Использование: /pdfpass <имя_файла.pdf> <пароль>")
            return
        filename, password = parts[1], parts[2]
        store_pdf_password(filename, password)
        await update.message.reply_text(f"Пароль сохранён для {filename}. Запустите /kb_sync, чтобы переиндексировать документ.")

    @only_allowed
    async def list_models(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            fetched = await self.openai.fetch_available_models()
            allowed = self.openai.allowed_models(fetched)
            keyboard, row = [], []
            for i, m in enumerate(allowed):
                row.append(InlineKeyboardButton(m, callback_data=f"setmodel:{m}"))
                if len(row) == 2:
                    keyboard.append(row); row = []
            if row: keyboard.append(row)
            await update.message.reply_text("Выбери модель:", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Не удалось получить список моделей: {e}")

    @only_allowed
    async def set_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("Использование: /set_model <model_name>")
            return
        model = parts[1].strip()
        try:
            fetched = await self.openai.fetch_available_models()
            allowed = self.openai.allowed_models(fetched)
            if model not in allowed:
                await update.message.reply_text("Эта модель не доступна. Используй /list_models")
                return
            self.openai.user_models[chat_id] = model
            await update.message.reply_text(f"Модель установлена: {model}")
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Не удалось установить модель: {e}")

    @only_allowed
    async def handle_model_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            q = update.callback_query
            chat_id = q.message.chat_id
            _, model = q.data.split(":", 1)
            fetched = await self.openai.fetch_available_models()
            allowed = self.openai.allowed_models(fetched)
            if model not in allowed:
                await q.answer("Эта модель недоступна", show_alert=True)
                return
            self.openai.user_models[chat_id] = model
            await q.answer("Модель установлена")
            await q.edit_message_text(f"Текущая модель: {model}")
        except Exception as e:
            capture_exception(e)
            await update.callback_query.answer("Ошибка")

    @only_allowed
    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.config.get("enable_image_generation", False):
            await update.message.reply_text("Генерация изображений отключена.")
            return
        text = (update.message.text or "").strip()
        parts = text.split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            await update.message.reply_text("Использование: /image <описание>")
            return
        prompt = parts[1].strip()
        try:
            url, size = await self.openai.generate_image(prompt)
            await update.message.reply_photo(url, caption=f"size: {size}")
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка генерации изображения: {e}")

    @only_allowed
    async def show_knowledge_base(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logging.warning(">>> Команда /kb вызвана")
        try:
            text = (update.message.text or "")
            query = text.partition(" ")[2].strip()
            chat_id = update.effective_chat.id
            if query:
                try:
                    selected_ids = self.ctx_manager.get_selected_documents(chat_id)
                    results = await self.retriever.search(query, top_k=5, document_ids=selected_ids or None)
                    if not results:
                        await update.message.reply_text("Ничего не найдено.")
                        return
                    reply = "Найдено:\n\n" + "\n\n---\n\n".join(r[1][:1000] for r in results)
                    await update.message.reply_text(reply[:4000])
                    return
                except Exception as e:
                    capture_exception(e)
                    logging.error("Ошибка поиска в retriever: %s", e, exc_info=True)
                    await update.message.reply_text("Ошибка поиска в базе знаний.")
                    return

            with SessionLocal() as s:
                docs = s.query(Document).order_by(Document.id).all()
            if not docs:
                await update.message.reply_text("База знаний пуста. Запустите /kb_sync")
                return

            keyboard = []
            selected = set(self.ctx_manager.get_selected_documents(chat_id))
            for d in docs[:100]:
                mark = "✅" if d.id in selected else "❌"
                btn = InlineKeyboardButton(f"{mark} {d.id} — {os.path.basename(d.path)}", callback_data=f"kbselect:{d.id}")
                keyboard.append([btn])

            await update.message.reply_text("Выберите документы для контекста:", reply_markup=InlineKeyboardMarkup(keyboard))

        except Exception as e:
            capture_exception(e)
            logging.error("Ошибка при получении списка файлов из базы знаний", exc_info=True)
            await update.message.reply_text("Не удалось загрузить базу знаний. Проверь токен или путь")

    @only_allowed
    async def handle_kb_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            q = update.callback_query
            data = q.data
            _, doc_id_str = data.split(":", 1)
            doc_id = int(doc_id_str)
            chat_id = q.message.chat_id

            current = set(self.ctx_manager.get_selected_documents(chat_id))
            if doc_id in current:
                current.remove(doc_id)
            else:
                current.add(doc_id)
            self.ctx_manager.set_selected_documents(chat_id, sorted(list(current)))

            with SessionLocal() as s:
                docs = s.query(Document).order_by(Document.id).all()

            keyboard = []
            for d in docs[:100]:
                mark = "✅" if d.id in current else "❌"
                btn = InlineKeyboardButton(f"{mark} {d.id} — {os.path.basename(d.path)}", callback_data=f"kbselect:{d.id}")
                keyboard.append([btn])

            await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
            await q.answer("Обновлено")
        except Exception as e:
            capture_exception(e)
            logging.error("handle_kb_selection failed", exc_info=True)
            await update.callback_query.answer("Ошибка")

    @only_allowed
    async def handle_password_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        if text.startswith("/"):
            return
        user_id = update.effective_user.id
        file_path = get_awaiting_password_file(user_id)
        if not file_path:
            return
        clear_awaiting_password(user_id)
        await update.message.reply_text(f"🔓 Пароль '{text}' принят для файла {file_path}. Запустите /kb_sync для переиндексации.")

    @only_allowed
    async def handle_file_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            doc = update.message.document
            file = await doc.get_file()
            local_path = await file.download_to_drive()
            chat_id = update.effective_chat.id
            prompt = f"Мне прислали документ {doc.file_name} ({doc.file_size} байт). Расскажи, что с ним можно сделать и какие следующие шаги предложишь."
            answer, usage = await self.openai.get_chat_response(chat_id, prompt)
            await update.message.reply_text(answer[:4000])
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка при анализе документа: {e}")

    @only_allowed
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            chat_id = update.effective_chat.id
            photo = update.message.photo[-1]
            file = await photo.get_file()
            file_bytes = await file.download_as_bytearray()
            import io
            bio = io.BytesIO(file_bytes)
            prompt = "Проанализируй изображение и ответь в контексте: предложи, что это, и как это можно использовать/интерпретировать."
            answer, usage = await self.openai.interpret_image(chat_id, bio, prompt=prompt)
            await update.message.reply_text(answer[:4000])
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка анализа изображения: {e}")

    @only_allowed
    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            voice = update.message.voice
            audio = update.message.audio
            file = None
            if voice:
                file = await voice.get_file()
            elif audio:
                file = await audio.get_file()
            else:
                return
            local_path = await file.download_to_drive()
            text = await self.openai.transcribe(str(local_path))
            chat_id = update.effective_chat.id
            answer, usage = await self.openai.get_chat_response(chat_id, text)
            await update.message.reply_text(f"🗣️ Ты сказал(а):\n{text[:2000]}\n\n💬 Мой ответ:\n{answer[:2000]}")
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка транскрибации/ответа: {e}")

    @only_allowed
    async def prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        query = (update.message.text or "").strip()
        try:
            answer, usage = await self.openai.get_chat_response(chat_id, query)
            await update.message.reply_text(answer)
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка: {e}")

    async def global_error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        capture_exception(context.error)
        logging.error("Exception:", exc_info=context.error)
