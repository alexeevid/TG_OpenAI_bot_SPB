import logging
from functools import wraps
from typing import Optional, List, Tuple

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

from sqlalchemy.orm import Session
from bot.db.session import SessionLocal
from bot.db.models import Document, Conversation
from bot.openai_helper import OpenAIHelper
from bot.settings import Settings

logger = logging.getLogger(__name__)

def only_allowed(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if self.allowed and uid not in self.allowed:
            await update.effective_message.reply_text("⛔ Доступ ограничен.")
            return
        return await func(self, update, context)
    return wrapper

class ChatGPTTelegramBot:
    def __init__(self, openai: OpenAIHelper, settings: Settings):
        self.openai = openai
        self.settings = settings
        self.allowed = set(settings.allowed_set) if settings.allowed_set else set()
        self.admins = set(settings.admin_set) if settings.admin_set else set()

    def install(self, app: Application):
        app.add_handler(CommandHandler("start", self.on_start))
        app.add_handler(CommandHandler("help", self.on_help))
        app.add_handler(CommandHandler("reset", self.on_reset))
        app.add_handler(CommandHandler("stats", self.on_stats))
        app.add_handler(CommandHandler("kb", self.on_kb))
        app.add_handler(CommandHandler("model", self.on_model))
        app.add_handler(CommandHandler("dialogs", self.on_dialogs))
        app.add_handler(CommandHandler("dialog", self.on_dialog_select))

        app.add_handler(CallbackQueryHandler(self.on_callback))

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        # Commands menu
        # Commands menu
        app.post_init = self._post_init_commands

    async def _post_init_commands(self, app: Application):
        cmds = [
            BotCommand("start", "Запуск и меню"),
            BotCommand("help", "Помощь"),
            BotCommand("reset", "Сброс контекста"),
            BotCommand("stats", "Статистика"),
            BotCommand("kb", "База знаний"),
            BotCommand("model", "Выбор модели"),
            BotCommand("dialogs", "Список диалогов"),
        ]
        try:
            await app.bot.set_my_commands(cmds)
        except Exception as e:
            logger.warning("Failed to set commands: %s", e)

    # ---------- Dialog helpers ----------
    def _get_db(self) -> Session:
        return SessionLocal()

    def _get_active_conv(self, chat_id: int, db: Session) -> Conversation:
        conv = db.query(Conversation).filter_by(chat_id=chat_id, is_active=True).order_by(Conversation.id.desc()).first()
        if not conv:
            conv = Conversation(chat_id=chat_id, title="Диалог")
            db.add(conv); db.commit(); db.refresh(conv)
        return conv

    # ---------- Commands ----------
    @only_allowed
    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет! Я готов к работе.\n"
            "Команды: /help, /reset, /stats, /kb, /model, /dialogs"
        )

    @only_allowed
    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "/reset — сброс контекста\n"
            "/stats — статистика\n"
            "/kb — база знаний (включить/исключить документы)\n"
            "/model — выбор модели OpenAI\n"
            "/dialogs — список диалогов, /dialog <id> — вернуться в диалог"
        )

    @only_allowed
    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        # Закрываем текущий активный диалог
        db.query(Conversation).filter_by(chat_id=chat_id, is_active=True).update({"is_active": False})
        db.commit()
        # Создаём новый
        newc = Conversation(chat_id=chat_id, title="Диалог")
        db.add(newc); db.commit()
        await update.message.reply_text("🔄 Новый диалог создан. Контекст очищен.")
        context.user_data.pop("kb_enabled", None)
        context.user_data.pop("kb_selected_ids", None)

    @only_allowed
    async def on_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        conv = self._get_active_conv(chat_id, db)
        docs_count = db.query(Document).count()
        selected = context.user_data.get("kb_selected_ids", set())
        model = self.openai.model
        await update.message.reply_text(
            f"📊 Статистика:\n"
            f"- Активный диалог: #{conv.id}\n"
            f"- Всего документов в БЗ: {docs_count}\n"
            f"- Выбрано: {len(selected)}\n"
            f"- Модель: {model}"
        )

    # ----- KB -----
    @only_allowed
    async def on_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        conv = self._get_active_conv(chat_id, db)

        kb_enabled = context.user_data.get("kb_enabled", True)
        selected = context.user_data.get("kb_selected_ids", set())
        docs = db.query(Document).order_by(Document.id.asc()).limit(30).all()

        rows = []
        for d in docs:
            mark = "✅" if d.id in selected else "➕"
            rows.append([InlineKeyboardButton(f"{mark} {d.title}", callback_data=f"kb_toggle:{d.id}")])

        # Admin-only sync button
        if update.effective_user and update.effective_user.id in self.admins:
            rows.append([InlineKeyboardButton("🔄 Синхронизировать с Я.Диском", callback_data="kb_sync")])

        rows.append([InlineKeyboardButton(("🔕 Отключить БЗ" if kb_enabled else "🔔 Включить БЗ"), callback_data="kb_toggle_enabled")])

        await update.message.reply_text(
            f"База знаний: {'включена' if kb_enabled else 'выключена'}.\n"
            "Выберите документы для контекста (до 30 показано).",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    # ----- Models -----
    @only_allowed
    async def on_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        models = self.openai.list_models()
        # Порежем список до 30 и оставим популярные
        prefer = [m for m in models if any(x in m for x in ["gpt-4o", "gpt-4.1", "gpt-4", "gpt-3.5"])]
        items = prefer[:30] if prefer else models[:30]
        rows = [[InlineKeyboardButton(m, callback_data=f"set_model:{m}")] for m in items]
        await update.message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(rows))

    # ----- Dialogs -----
    @only_allowed
    async def on_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        items = db.query(Conversation).filter_by(chat_id=chat_id).order_by(Conversation.id.desc()).limit(10).all()
        if not items:
            await update.message.reply_text("Нет сохранённых диалогов.")
            return
        rows = [[InlineKeyboardButton(f"#{c.id} {c.title}", callback_data=f"goto_dialog:{c.id}") ] for c in items]
        await update.message.reply_text("Выберите диалог:", reply_markup=InlineKeyboardMarkup(rows))

    @only_allowed
    async def on_dialog_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # /dialog <id>
        args = context.args or []
        if not args:
            await update.message.reply_text("Использование: /dialog <id>")
            return
        try:
            target = int(args[0])
        except ValueError:
            await update.message.reply_text("Некорректный id.")
            return

        db = self._get_db()
        chat_id = update.effective_chat.id
        # деактивируем текущий
        db.query(Conversation).filter_by(chat_id=chat_id, is_active=True).update({"is_active": False})
        # активируем нужный
        c = db.query(Conversation).filter_by(chat_id=chat_id, id=target).first()
        if not c:
            await update.message.reply_text("Диалог не найден.")
            return
        c.is_active = True
        db.commit()
        await update.message.reply_text(f"✅ Активирован диалог #{c.id} ({c.title}).")

    # ----- Callbacks -----
    @only_allowed
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        data = q.data or ""
        if data.startswith("kb_toggle:"):
            doc_id = int(data.split(":")[1])
            selected = context.user_data.get("kb_selected_ids", set())
            if doc_id in selected:
                selected.remove(doc_id)
            else:
                selected.add(doc_id)
            context.user_data["kb_selected_ids"] = selected
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("Изменения применены. Нажмите /kb, чтобы обновить список.")
        elif data == "kb_toggle_enabled":
            cur = context.user_data.get("kb_enabled", True)
            context.user_data["kb_enabled"] = not cur
            await q.edit_message_text(f"База знаний: {'включена' if not cur else 'выключена'}. Нажмите /kb, чтобы обновить.")
        elif data == "kb_sync":
            if update.effective_user and update.effective_user.id in self.admins:
                await q.edit_message_text("Запускаю синхронизацию…")
                await self._kb_sync_internal(update, context)
            else:
                await q.edit_message_text("Доступно только администратору.")
        elif data.startswith("set_model:"):
            m = data.split(":", 1)[1]
            self.openai.set_model(m)
            await q.edit_message_text(f"Модель установлена: {m}")
        elif data.startswith("goto_dialog:"):
            # same as /dialog select by id
            try:
                target = int(data.split(":")[1])
            except ValueError:
                return
            db = self._get_db()
            chat_id = update.effective_chat.id
            db.query(Conversation).filter_by(chat_id=chat_id, is_active=True).update({"is_active": False})
            c = db.query(Conversation).filter_by(chat_id=chat_id, id=target).first()
            if c:
                c.is_active = True; db.commit()
                await q.edit_message_text(f"✅ Активирован диалог #{c.id} ({c.title}).")
            else:
                await q.edit_message_text("Диалог не найден.")

    async def _kb_sync_internal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        from bot.knowledge_base.indexer import sync_disk_to_db
        from bot.db.session import SessionLocal
        db = SessionLocal()
        added = 0
        try:
            added = sync_disk_to_db(db, self.settings.yandex_disk_token, self.settings.yandex_root_path)
            await update.effective_chat.send_message(f"Готово. Добавлено файлов: {added}")
        except Exception as e:
            await update.effective_chat.send_message(f"Ошибка синхронизации: {e}")
        finally:
            db.close()

    # ----- Text handler -----
    @only_allowed
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        conv = self._get_active_conv(chat_id, db)

        kb_enabled = context.user_data.get("kb_enabled", True)
        selected_ids = context.user_data.get("kb_selected_ids", set())
        selected_docs: List[Document] = []
        if kb_enabled and selected_ids:
            selected_docs = db.query(Document).filter(Document.id.in_(list(selected_ids))).all()

        system_hint = "Ты — помощник. Отвечай кратко и по делу."
        kb_hint = ""
        if selected_docs:
            titles = ", ".join([d.title for d in selected_docs][:10])
            kb_hint = f" Учитывай информацию из документов: {titles}."
        prompt = (update.message.text or "").strip()

        messages = [
            {"role": "system", "content": system_hint + kb_hint},
            {"role": "user", "content": prompt}
        ]

        try:
            ans = self.openai.chat(messages)
        except Exception as e:
            await update.message.reply_text(f"Ошибка обращения к OpenAI: {e}")
            return

        await update.message.reply_text(ans or "Пустой ответ.")
