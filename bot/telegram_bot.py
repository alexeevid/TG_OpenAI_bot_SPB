import logging
import time
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
import os
import asyncio
from io import BytesIO
from datetime import datetime
from bot.settings import settings

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

    # =========================
    # /reset — сброс диалога
    # =========================
    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сброс текущего диалога и состояния KB."""
        user_id = update.effective_user.id
        self.current_dialog_by_user.pop(user_id, None)
        self.awaiting_rename.pop(user_id, None)
        self.awaiting_kb_pwd.pop(user_id, None)
    
        # Сброс в DialogManager (если реализован)
        if hasattr(self.dialog_manager, "reset_user_dialogs"):
            self.dialog_manager.reset_user_dialogs(user_id)
    
        await update.message.reply_text("🔄 Диалог и настройки базы знаний сброшены.")
    
    # =========================
    # /model — выбор модели
    # =========================
    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Динамический выбор модели OpenAI по списку из API."""
        user_id = update.effective_user.id
        current_dlg = self.current_dialog_by_user.get(user_id)
        if not current_dlg:
            dlg = self.dialog_manager.create_dialog(user_id)
            self.current_dialog_by_user[user_id] = dlg.id
            current_dlg = dlg.id
    
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.settings.openai_api_key)
    
            models_data = client.models.list()
            model_names = sorted([m.id for m in models_data.data])
    
            if not model_names:
                await update.message.reply_text("⚠️ Не удалось получить список моделей OpenAI.")
                return
    
            buttons = []
            for i in range(0, len(model_names), 2):
                row = []
                for model in model_names[i:i+2]:
                    row.append(InlineKeyboardButton(model, callback_data=f"model:{model}"))
                buttons.append(row)
    
            await update.message.reply_text(
                "Выберите модель для работы:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
    
        except Exception as e:
            await update.message.reply_text(f"⚠️ Ошибка при получении списка моделей: {e}")
    
    # =========================
    # /mode — выбор стиля
    # =========================
    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выбор стиля ответа ассистента."""
        user_id = update.effective_user.id
        current_dlg = self.current_dialog_by_user.get(user_id)
        if not current_dlg:
            dlg = self.dialog_manager.create_dialog(user_id)
            self.current_dialog_by_user[user_id] = dlg.id
            current_dlg = dlg.id
    
        styles = {
            "ceo": "СЕО — стратегично и кратко",
            "expert": "Эксперт — глубоко и с фактами",
            "pro": "Про — по делу и структурно",
            "user": "Юзер — просто и понятно"
        }
    
        buttons = [
            [InlineKeyboardButton(desc, callback_data=f"mode:{key}")]
            for key, desc in styles.items()
        ]
    
        await update.message.reply_text(
            "Выберите стиль общения:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    
    
    # =========================
    # /img — генерация изображений
    # =========================
    async def cmd_img(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Генерация изображения по запросу."""
        prompt = " ".join(context.args) if context.args else None
    
        if not prompt:
            await update.message.reply_text("🖼 Укажите описание. Пример:\n/img космический корабль на Марсе")
            return
    
        try:
            await update.message.reply_text("⏳ Генерирую изображение, подождите...")
            img_bytes = await self.openai.generate_image(prompt)
            if not img_bytes:
                await update.message.reply_text("⚠️ Не удалось сгенерировать изображение.")
                return
    
            await update.message.reply_photo(photo=img_bytes, caption=f"🖼 {prompt}")
    
        except Exception as e:
            await update.message.reply_text(f"⚠️ Ошибка генерации изображения: {e}")

    
    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выбор стиля ответа ассистента."""
        user_id = update.effective_user.id
        current_dlg = self.current_dialog_by_user.get(user_id)
        if not current_dlg:
            dlg = self.dialog_manager.create_dialog(user_id)
            self.current_dialog_by_user[user_id] = dlg.id
            current_dlg = dlg.id
    
        styles = {
            "ceo": "СЕО — стратегичный, кратко о главном",
            "expert": "Эксперт — детально, с фактами и примерами",
            "pro": "Про — чётко, по делу, без воды",
            "user": "Юзер — просто и понятно, без терминов"
        }
    
        buttons = [
            [InlineKeyboardButton(desc, callback_data=f"mode:{key}")]
            for key, desc in styles.items()
        ]
    
        await update.message.reply_text(
            "Выберите стиль общения:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    
    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Динамический выбор модели OpenAI по списку из API."""
        user_id = update.effective_user.id
        current_dlg = self.current_dialog_by_user.get(user_id)
        if not current_dlg:
            dlg = self.dialog_manager.create_dialog(user_id)
            self.current_dialog_by_user[user_id] = dlg.id
            current_dlg = dlg.id
    
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.settings.openai_api_key)
    
            # Запрос списка моделей
            models_data = client.models.list()
            model_names = sorted([m.id for m in models_data.data])
    
            if not model_names:
                await update.message.reply_text("⚠️ Не удалось получить список моделей OpenAI.")
                return
    
            # Делаем кнопки по 2 в строке
            buttons = []
            for i in range(0, len(model_names), 2):
                row = []
                for model in model_names[i:i+2]:
                    row.append(InlineKeyboardButton(model, callback_data=f"model:{model}"))
                buttons.append(row)
    
            await update.message.reply_text(
                "Выберите модель для работы:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        except Exception as e:
            await update.message.reply_text(f"⚠️ Ошибка при получении списка моделей: {e}")
    
    async def cmd_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        dlg_id = self.current_dialog_by_user.get(user_id)
        dlg_state = self.dialog_manager.get_dialog_state(dlg_id, user_id)
    
        from bot.settings import settings  # импорт в начале файла
        is_admin = settings.admin_user_ids and user_id in settings.admin_user_ids
    
        docs = await asyncio.to_thread(self.kb_indexer.list_documents)
        dlg_state.kb_last_paths = {i: d.path for i, d in enumerate(docs)}
    
        buttons = []
        for i, d in enumerate(docs):
            selected = d.path in (dlg_state.kb_documents or [])
            mark = "✅ " if selected else "☐ "
            buttons.append([
                InlineKeyboardButton(f"{mark}{os.path.basename(d.path)}", callback_data=f"kb:toggle:{i}")
            ])
            if selected and d.path.lower().endswith(".pdf"):
                buttons.append([
                    InlineKeyboardButton("🔑 Пароль", callback_data=f"kb:pwd:{i}")
                ])
    
        buttons.append([InlineKeyboardButton("💾 Сохранить выбор", callback_data="kb:save")])
        if is_admin:
            buttons.append([InlineKeyboardButton("🔁 Повторить синхронизацию", callback_data="kb:resync")])
    
        await update.message.reply_text("📚 Выберите документы из базы знаний:", reply_markup=InlineKeyboardMarkup(buttons))

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает статистику диалогов и сообщений пользователя."""
        user_id = update.effective_user.id

        dialogs = self.dialog_manager.get_active_dialogs(user_id)
        total_dialogs = len(dialogs)
        total_messages = 0

        for dlg in dialogs:
            msgs = self.dialog_manager.get_messages(dlg.id, limit=999999)
            total_messages += len(msgs)

        await update.message.reply_text(
            f"📊 Ваша статистика:\n"
            f"— Активных диалогов: {total_dialogs}\n"
            f"— Всего сообщений: {total_messages}"
        )
    
    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сброс текущего диалога и состояния KB."""
        user_id = update.effective_user.id
        self.current_dialog_by_user.pop(user_id, None)
        self.awaiting_rename.pop(user_id, None)
        self.awaiting_kb_pwd.pop(user_id, None)
    
        # Если используем DialogManager, очищаем состояние
        self.dialog_manager.reset_user_dialogs(user_id)
    
        await update.message.reply_text("🔄 Диалог и настройки базы знаний сброшены.")
    
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
        data = (query.data or "").strip()
        user_id = update.effective_user.id
        current_dlg = self.current_dialog_by_user.get(user_id)
    
        # === Диалоги ===
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
    
        # === Модель и стиль ===
        if data.startswith("model:"):
            model_name = data.split(":", 1)[1]
            dlg_state = self.dialog_manager.get_dialog_state(current_dlg, user_id)
            dlg_state.model = model_name
            self.dialog_manager.save_dialog_state(current_dlg, user_id, dlg_state)
            await query.edit_message_text(f"✅ Модель установлена: {model_name}")
            return
    
        if data.startswith("mode:"):
            mode_key = data.split(":", 1)[1]
            dlg_state = self.dialog_manager.get_dialog_state(current_dlg, user_id)
            dlg_state.style = mode_key
            self.dialog_manager.save_dialog_state(current_dlg, user_id, dlg_state)
            await query.edit_message_text(f"✅ Стиль установлен: {mode_key}")
            return
    
        # === KB: выбор/отключение документов ===
        # === KB CALLBACKS ===
        if data.startswith("kb:toggle:"):
            idx = int(data.split(":", 2)[2])
            dlg_id = self.current_dialog_by_user.get(user_id)
            dlg_state = self.dialog_manager.get_dialog_state(dlg_id, user_id)
            path = dlg_state.kb_last_paths.get(idx)

            if not path:
                await query.answer("Элемент недоступен, обновите список (/kb).", show_alert=True)
                return

            if path in (dlg_state.kb_documents or []):
                dlg_state.kb_documents.remove(path)
                dlg_state.kb_passwords.pop(path, None)
            else:
                dlg_state.kb_documents.append(path)

            self.dialog_manager.save_dialog_state(dlg_id, user_id, dlg_state)

            docs = await asyncio.to_thread(self.kb_indexer.list_documents)
            buttons = []
            for i, d in enumerate(docs):
                selected = d.path in (dlg_state.kb_documents or [])
                mark = "✅ " if selected else "☐ "
                buttons.append([
                    InlineKeyboardButton(f"{mark}{os.path.basename(d.path)}", callback_data=f"kb:toggle:{i}")
                ])
                if selected and d.path.lower().endswith(".pdf"):
                    buttons.append([
                        InlineKeyboardButton("🔑 Пароль", callback_data=f"kb:pwd:{i}")
                    ])

            buttons.append([InlineKeyboardButton("💾 Сохранить выбор", callback_data="kb:save")])
            from bot.settings import settings
            is_admin = settings.admin_user_ids and user_id in settings.admin_user_ids
            if is_admin:
                buttons.append([InlineKeyboardButton("🔁 Повторить синхронизацию", callback_data="kb:resync")])

            await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
            return

        elif data == "kb:save":
            dlg_id = self.current_dialog_by_user.get(user_id)
            dlg_state = self.dialog_manager.get_dialog_state(dlg_id, user_id)
            dlg_state.kb_enabled = bool(dlg_state.kb_documents)
            self.dialog_manager.save_dialog_state(dlg_id, user_id, dlg_state)
            await query.edit_message_text(
                "📂 База знаний отключена." if not dlg_state.kb_enabled
                else f"Выбрано документов: {len(dlg_state.kb_documents)}. БЗ включена."
            )
            return

        elif data == "kb:resync":
            from bot.settings import settings
            if not (settings.admin_user_ids and user_id in settings.admin_user_ids):
                await query.answer("⛔ Синхронизация доступна только администраторам.", show_alert=True)
                return
            await query.edit_message_text("🔄 Синхронизация базы знаний...")
            await asyncio.to_thread(self.kb_indexer.sync)
            await asyncio.sleep(1)
            await self.cmd_kb(update, context)
            return

        elif data.startswith("kb:pwd:"):
            idx = int(data.split(":", 2)[2])
            dlg_id = self.current_dialog_by_user.get(user_id)
            dlg_state = self.dialog_manager.get_dialog_state(dlg_id, user_id)
            path = dlg_state.kb_last_paths.get(idx)

            if not path:
                await query.answer("Элемент недоступен, обновите список (/kb).", show_alert=True)
                return

            self.awaiting_kb_pwd[user_id] = idx
            await query.edit_message_text(f"Введите пароль для документа: {os.path.basename(path)}")
            return
    
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        text = update.message.text.strip()
    
        # === Переименование диалога ===
        if user_id in self.awaiting_rename:
            dlg_id = self.awaiting_rename.pop(user_id)
            self.dialog_manager.rename_dialog(dlg_id, user_id, text)
            await update.message.reply_text(f"Диалог переименован в: {text}")
            return
    
        # === Пароль к документу KB ===
        if user_id in self.awaiting_kb_pwd:
            idx = self.awaiting_kb_pwd.pop(user_id)
            dlg_id = self.current_dialog_by_user.get(user_id)
            dlg_state = self.dialog_manager.get_dialog_state(dlg_id, user_id)
            path = dlg_state.kb_last_paths.get(idx)
            if path:
                dlg_state.kb_passwords[path] = text
                self.dialog_manager.save_dialog_state(dlg_id, user_id, dlg_state)
                await update.message.reply_text(f"🔑 Пароль сохранён для: {os.path.basename(path)}")
            else:
                await update.message.reply_text("⚠️ Не удалось сохранить пароль: документ не найден.")
            return
    
        # === Определение текущего диалога ===
        current_dlg = self.current_dialog_by_user.get(user_id)
        if not current_dlg:
            dlg = self.dialog_manager.create_dialog(user_id)
            self.current_dialog_by_user[user_id] = dlg.id
            current_dlg = dlg.id
    
        self.dialog_manager.add_message(current_dlg, "user", text)
    
        # === Подготовка контекста из БЗ ===
        kb_context = None
        dlg_state = self.dialog_manager.get_dialog_state(current_dlg, user_id)
    
        if dlg_state.kb_enabled and dlg_state.kb_documents:
            chunks = await asyncio.to_thread(
                self.kb_retriever.retrieve,
                text,
                dlg_state.kb_documents,
                dlg_state.kb_passwords
            )
    
            # Проверка PDF-документов на успешность дешифровки
            used_text = "\n".join(chunks) if chunks else ""
            for path in dlg_state.kb_documents:
                if path.lower().endswith(".pdf") and path not in used_text:
                    await update.message.reply_text(
                        f"⚠️ Документ {os.path.basename(path)} не использован. "
                        "Возможно, необходим пароль или он указан неверно."
                    )
    
            if chunks:
                kb_context = "\n\n".join(chunks)
    
        # === Запрос к OpenAI ===
        dlg_obj = self.dialog_manager.get_dialog(current_dlg, user_id)
        reply = await self.openai.chat(
            dialog_id=current_dlg,
            user_id=user_id,
            user_message=text,
            style=dlg_obj.style,
            kb_context=kb_context,
            model=dlg_obj.model
        )
    
        self.dialog_manager.add_message(current_dlg, "assistant", reply)
        await update.message.reply_text(reply)
