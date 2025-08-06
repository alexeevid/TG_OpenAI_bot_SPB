import logging
import time
import os
from io import BytesIO
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    InputFile
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)

from .dialog_manager import DialogManager
from .openai_helper import OpenAIHelper
from .knowledge_base.indexer import KnowledgeBaseIndexer
from .knowledge_base.retriever import KnowledgeBaseRetriever

logger = logging.getLogger(__name__)


class ChatGPTTelegramBot:
    def __init__(self, settings):
        self.settings = settings
        self.dialog_manager = DialogManager()
        self.openai = OpenAIHelper(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            image_model=settings.image_model
        )
        self.kb_indexer = KnowledgeBaseIndexer(settings)
        self.kb_retriever = KnowledgeBaseRetriever(settings)

        self.current_dialog_by_user = {}
        self.awaiting_rename = {}
        self.awaiting_kb_pwd = {}

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Привет! Я готов к работе.")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Список команд: /dialogs /rename /export /kb /kb_diag /model /mode /img /web /stats")

    async def cmd_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        dialogs = self.dialog_manager.get_active_dialogs(user_id)

        buttons = []
        for dlg in dialogs:
            title = f"{dlg.title} | {dlg.created_at.strftime('%Y.%m.%d')}"
            buttons.append([
                InlineKeyboardButton(title, callback_data=f"dlg:open:{dlg.id}"),
                InlineKeyboardButton("✏️", callback_data=f"dlg:rename:{dlg.id}"),
                InlineKeyboardButton("📤", callback_data=f"dlg:export:{dlg.id}"),
                InlineKeyboardButton("🗑️", callback_data=f"dlg:del:{dlg.id}")
            ])
        buttons.append([InlineKeyboardButton("➕ Новый диалог", callback_data="dlg:new")])

        await update.message.reply_text(
            "Ваши диалоги:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def cmd_rename(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        current_dlg = self.current_dialog_by_user.get(user_id)
        if not current_dlg:
            await update.message.reply_text("Нет активного диалога.")
            return
        self.awaiting_rename[user_id] = current_dlg
        await update.message.reply_text("Введите новое название диалога:")

    async def cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        current_dlg = self.current_dialog_by_user.get(user_id)
        if not current_dlg:
            await update.message.reply_text("Нет активного диалога.")
            return
        md_text = self.dialog_manager.export_dialog(current_dlg, user_id)
        if not md_text:
            await update.message.reply_text("Диалог пуст.")
            return
        file_bytes = BytesIO(md_text.encode("utf-8"))
        file_bytes.name = f"dialog_{current_dlg}.md"
        await update.message.reply_document(InputFile(file_bytes))

    async def cmd_kb_diag(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        current_dlg = self.current_dialog_by_user.get(user_id)
        if not current_dlg:
            await update.message.reply_text("Нет активного диалога.")
            return
        dlg = self.dialog_manager.get_dialog(current_dlg, user_id)
        msgs = self.dialog_manager.get_messages(current_dlg, limit=5)
        kb_docs = dlg.kb_documents if hasattr(dlg, "kb_documents") else []

        text = f"Диалог: {dlg.title} (ID {dlg.id})\nВыбранные документы: {', '.join(kb_docs) or '—'}\nПоследние сообщения:\n"
        for m in reversed(msgs):
            text += f"[{m.role}] {m.content}\n"
        await update.message.reply_text(text)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        user_id = update.effective_user.id

        if data == "dlg:new":
            dlg = self.dialog_manager.create_dialog(user_id)
            self.current_dialog_by_user[user_id] = dlg.id
            await query.edit_message_text(f"Создан новый диалог: {dlg.title}")
            return

        if data.startswith("dlg:open:"):
            dlg_id = int(data.split(":")[2])
            self.current_dialog_by_user[user_id] = dlg_id
            dlg = self.dialog_manager.get_dialog(dlg_id, user_id)
            last_msg = self.dialog_manager.get_messages(dlg_id, limit=1)
            last_date = last_msg[0].timestamp.strftime("%Y.%m.%d") if last_msg else "—"
            await query.edit_message_text(f"📂 Диалог '{dlg.title}' (последнее сообщение: {last_date}) активирован.")
            return

        if data.startswith("dlg:rename:"):
            dlg_id = int(data.split(":")[2])
            self.awaiting_rename[user_id] = dlg_id
            await query.edit_message_text("Введите новое название диалога:")
            return

        if data.startswith("dlg:export:"):
            dlg_id = int(data.split(":")[2])
            md_text = self.dialog_manager.export_dialog(dlg_id, user_id)
            if not md_text:
                await query.edit_message_text("Диалог пуст.")
                return
            file_bytes = BytesIO(md_text.encode("utf-8"))
            file_bytes.name = f"dialog_{dlg_id}.md"
            await context.bot.send_document(chat_id=user_id, document=InputFile(file_bytes))
            return

        if data.startswith("dlg:del:"):
            dlg_id = int(data.split(":")[2])
            self.dialog_manager.soft_delete_dialog(dlg_id, user_id)
            await query.edit_message_text("Диалог удалён.")
            return

        if data.startswith("kb:pwd:"):
            idx = int(data.split(":")[2])
            self.awaiting_kb_pwd[user_id] = idx
            await query.edit_message_text("Введите пароль к документу:")
            return

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        text = update.message.text.strip()

        if user_id in self.awaiting_rename:
            dlg_id = self.awaiting_rename.pop(user_id)
            self.dialog_manager.rename_dialog(dlg_id, user_id, text)
            await update.message.reply_text(f"Диалог переименован в: {text}")
            return

        if user_id in self.awaiting_kb_pwd:
            idx = self.awaiting_kb_pwd.pop(user_id)
            # Здесь логика сохранения пароля
            await update.message.reply_text("🔑 Пароль сохранён.")
            return

        current_dlg = self.current_dialog_by_user.get(user_id)
        if not current_dlg:
            dlg = self.dialog_manager.create_dialog(user_id)
            self.current_dialog_by_user[user_id] = dlg.id
            current_dlg = dlg.id

        self.dialog_manager.add_message(current_dlg, "user", text)
        kb_ctx = None
        dlg_obj = self.dialog_manager.get_dialog(current_dlg, user_id)

        reply = await self.openai.chat(
            dialog_id=current_dlg,
            user_id=user_id,
            user_message=text,
            style=dlg_obj.style,
            kb_context=kb_ctx,
            model=dlg_obj.model
        )

        self.dialog_manager.add_message(current_dlg, "assistant", reply)
        await update.message.reply_text(reply)
