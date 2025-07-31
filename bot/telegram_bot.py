# bot/telegram_bot.py
from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllChatAdministrators,
    InputFile,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# ----- Опциональная База знаний (KB) -----
KB_AVAILABLE: bool = True
try:
    from bot.knowledge_base.indexer import KnowledgeBaseIndexer, KBMeta
    from bot.knowledge_base.retriever import KnowledgeBaseRetriever, KBChunk
    from bot.knowledge_base.context_manager import ContextManager
except Exception as e:
    KB_AVAILABLE = False
    logger.warning("KB unavailable: %s", e)


# ----- Состояние диалога (in-memory) -----
@dataclass
class DialogState:
    dialog_id: int
    title: str = "Диалог"
    created_at_ts: float = field(default_factory=lambda: time.time())
    updated_at_ts: float = field(default_factory=lambda: time.time())
    model: Optional[str] = None
    style: str = "Pro"  # Pro | Expert | User | CEO
    kb_selected_ids: List[str] = field(default_factory=list)


class ChatGPTTelegramBot:
    def __init__(self, openai, settings):
        self.openai = openai
        self.settings = settings

        # Разрешения
        self.admin_ids = set(getattr(settings, "admin_user_ids", []) or getattr(settings, "admin_set", []) or [])
        self.allowed_ids = set(getattr(settings, "allowed_user_ids", []) or getattr(settings, "allowed_set", []) or [])

        # Диалоги на пользователя
        self._dialogs_by_user: Dict[int, Dict[int, DialogState]] = {}
        self._current_dialog_by_user: Dict[int, int] = {}
        self._next_dialog_id: int = 1

        # KB
        self.kb_indexer: Optional[KnowledgeBaseIndexer] = None
        self.kb_retriever: Optional[KnowledgeBaseRetriever] = None
        self.kb_ctx: Optional[ContextManager] = None
        if KB_AVAILABLE:
            try:
                self.kb_indexer = KnowledgeBaseIndexer(settings)
                self.kb_retriever = KnowledgeBaseRetriever(settings)
                self.kb_ctx = ContextManager(settings)
            except Exception as e:
                KB_AVAILABLE = False  # локально помечаем
                logger.exception("KB init failed: %s", e)

    # ---------- Команды/меню ----------
    def _build_commands(self) -> List[BotCommand]:
        return [
            BotCommand("start", "начать"),
            BotCommand("help", "помощь"),
            BotCommand("reset", "новый диалог (сброс контекста)"),
            BotCommand("stats", "статистика"),
            BotCommand("kb", "база знаний (выбор документов)"),
            BotCommand("model", "выбор модели"),
            BotCommand("mode", "стиль ответов"),
            BotCommand("dialogs", "мои диалоги"),
            BotCommand("img", "сгенерировать изображение"),
            BotCommand("web", "веб-поиск с источниками"),
        ]

    async def setup_commands(self, app: Application) -> None:
        """
        Вызывается builder.post_init(...) из main.py. Ставит команды во всех scope.
        """
        commands = self._build_commands()
        bot = app.bot

        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception:
            pass

        scopes = [BotCommandScopeDefault(), BotCommandScopeAllPrivateChats(), BotCommandScopeAllChatAdministrators()]

        # Сначала почистим, затем выставим
        for scope in scopes:
            try:
                await bot.delete_my_commands(scope=scope)
            except Exception:
                pass
            await bot.set_my_commands(commands=commands, scope=scope)

        logger.info("✅ Команды установлены (global scopes)")

    # ---------- Регистрация обработчиков ----------
    def install(self, app: Application) -> None:
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("reset", self.cmd_reset))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("dialogs", self.cmd_dialogs))
        app.add_handler(CommandHandler("img", self.cmd_img))
        app.add_handler(CommandHandler("web", self.cmd_web))
        app.add_handler(CommandHandler("kb", self.cmd_kb))

        # Сообщения пользователя
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file_or_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        # Inline callbacks
        app.add_handler(CallbackQueryHandler(self.on_callback))

    # ---------- Вспомогательные ----------
    def _ensure_dialog(self, user_id: int) -> DialogState:
        user_dialogs = self._dialogs_by_user.setdefault(user_id, {})
        if user_id not in self._current_dialog_by_user:
            dlg_id = self._next_dialog_id
            self._next_dialog_id += 1
            st = DialogState(dialog_id=dlg_id)
            user_dialogs[dlg_id] = st
            self._current_dialog_by_user[user_id] = dlg_id
        return user_dialogs[self._current_dialog_by_user[user_id]]

    def _list_dialogs(self, user_id: int) -> List[DialogState]:
        return list(self._dialogs_by_user.get(user_id, {}).values())

    async def _send_action(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, action: ChatAction = ChatAction.TYPING):
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=action)
        except Exception:
            pass

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids if self.admin_ids else False

    # ---------- Команды ----------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_dialog(update.effective_user.id)
        await update.effective_message.reply_text(
            "Привет! Я готов к работе.\n"
            "Доступные команды: /help, /reset, /stats, /kb, /model, /mode, /dialogs, /img, /web"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "/reset — новый диалог (сброс контекста)\n"
            "/stats — статистика\n"
            "/kb — база знаний (выбор документов)\n"
            "/model — выбор модели OpenAI\n"
            "/mode — стиль ответов (Pro/Expert/User/CEO)\n"
            "/dialogs — список диалогов (открыть/удалить/новый)\n"
            "/img — сгенерировать изображение\n"
            "/web — веб-поиск (верну ссылки-источники)"
        )
        await update.effective_message.reply_text(text)

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        dlg_id = self._next_dialog_id
        self._next_dialog_id += 1
        self._dialogs_by_user.setdefault(user_id, {})[dlg_id] = DialogState(dialog_id=dlg_id)
        self._current_dialog_by_user[user_id] = dlg_id
        await update.effective_message.reply_text("🔄 Новый диалог создан. Контекст очищен.")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        kb_state = "доступна" if KB_AVAILABLE and self.kb_indexer and self.kb_retriever and self.kb_ctx else "недоступна"
        selected = ", ".join(st.kb_selected_ids) if st.kb_selected_ids else "—"
        vector_mode = None
        if KB_AVAILABLE and self.kb_retriever:
            try:
                vector_mode = "да" if self.kb_retriever.is_vector_store() else "нет"
            except Exception:
                vector_mode = "нет"
        text = (
            "📊 Статистика:\n"
            f"- Диалог: {st.title}\n"
            f"- Модель: {st.model or getattr(self.settings, 'openai_model', 'gpt-4o')}\n"
            f"- Стиль: {st.style}\n"
            f"- База знаний: {kb_state}\n"
            f"- Выбранные документы: {selected}\n"
            f"- Векторный поиск (pgvector): {vector_mode or '—'}"
        )
        await update.effective_message.reply_text(text)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        try:
            models = self.openai.list_models_for_menu()
        except Exception as e:
            logger.exception("list_models failed: %s", e)
            await update.effective_message.reply_text("Не удалось получить список моделей.")
            return

        current = st.model or getattr(self.settings, "openai_model", None)
        rows: List[List[InlineKeyboardButton]] = []
        for name, is_locked in models:
            mark = "✅ " if name == current else ""
            lock = " 🔒" if is_locked else ""
            rows.append([InlineKeyboardButton(f"{mark}{name}{lock}", callback_data=f"model:{name}")])
        await update.effective_message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        modes = ["Pro", "Expert", "User", "CEO"]
        rows = []
        for m in modes:
            mark = "✅ " if st.style == m else ""
            rows.append([InlineKeyboardButton(f"{mark}{m}", callback_data=f"mode:{m}")])
        await update.effective_message.reply_text("Выберите стиль ответа:", reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        dialogs = self._list_dialogs(user_id)
        current_id = self._current_dialog_by_user.get(user_id)
        if not dialogs:
            await update.effective_message.reply_text("Диалогов пока нет. Нажмите /reset для создания нового.")
            return

        rows = []
        for d in dialogs:
            title = d.title or f"Диалог #{d.dialog_id}"
            prefix = "⭐ " if d.dialog_id == current_id else ""
            rows.append([
                InlineKeyboardButton(f"{prefix}{title}", callback_data=f"open:{d.dialog_id}"),
                InlineKeyboardButton("🗑️", callback_data=f"del:{d.dialog_id}"),
            ])
        rows.append([InlineKeyboardButton("➕ Новый диалог", callback_data="newdlg")])
        await update.effective_message.reply_text("Выберите диалог:", reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_img(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.effective_message.reply_text("Использование: /img <описание изображения>")
            return
        prompt = " ".join(context.args)
        await self._send_action(update.effective_chat.id, context, ChatAction.UPLOAD_PHOTO)
        try:
            img_bytes, used_prompt = await asyncio.to_thread(self.openai.generate_image, prompt, None)
            file = InputFile(io.BytesIO(img_bytes), filename="image.png")
            await update.effective_message.reply_photo(photo=file, caption=f"🖼️ Prompt:\n{used_prompt}")
        except Exception as e:
            logger.exception("Image generation failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка генерации изображения: {e}")

    async def cmd_web(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.effective_message.reply_text("Использование: /web <запрос>")
            return
        query = " ".join(context.args)
        await self._send_action(update.effective_chat.id, context, ChatAction.TYPING)
        try:
            answer, sources = await asyncio.to_thread(self.openai.web_answer, query)
            if sources:
                src_text = "\n\nИсточники:\n" + "\n".join(f"• {u}" for u in sources)
            else:
                src_text = "\n\n⚠️ Не удалось получить явные ссылки-источники."
            await update.effective_message.reply_text(answer + src_text)
        except Exception as e:
            logger.exception("Web search failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка веб-поиска: {e}")

    async def cmd_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Логика по ТЗ:
        - если админ — автоматически запускаем синхронизацию;
        - показываем список документов БЗ с чекбоксами;
        - наличие отмеченных документов = БЗ "включена" в диалоге.
        """
        st = self._ensure_dialog(update.effective_user.id)
        if not (KB_AVAILABLE and self.kb_indexer and self.kb_retriever and self.kb_ctx):
            await update.effective_message.reply_text("Модуль базы знаний недоступен в этой сборке.")
            return

        # Автосинхронизация для админа
        if self._is_admin(update.effective_user.id):
            try:
                added, updated, deleted, unchanged = await asyncio.to_thread(self.kb_indexer.sync)
                sync_summary = (
                    f"Синхронизация:\n"
                    f"• Добавлено: {added}\n• Обновлено: {updated}\n• Удалено: {deleted}\n• Без изменений: {unchanged}\n\n"
                )
            except Exception as e:
                logger.exception("KB sync failed: %s", e)
                sync_summary = f"Синхронизация не удалась: {e}\n\n"
        else:
            sync_summary = ""

        # Список документов
        try:
            docs: List[KBMeta] = await asyncio.to_thread(self.kb_indexer.list_documents)
        except Exception as e:
            logger.exception("KB list_documents failed: %s", e)
            await update.effective_message.reply_text("Не удалось получить список документов.")
            return

        rows: List[List[InlineKeyboardButton]] = []
        if docs:
            for d in docs:
                checked = "✅ " if d.id in st.kb_selected_ids else ""
                title = d.title or d.path or d.id
                label = f"{checked}{title}"
                rows.append([InlineKeyboardButton(label, callback_data=f"kb:toggle:{d.id}")])
        else:
            rows.append([InlineKeyboardButton("Документы не найдены", callback_data="kb:none")])

        rows.append([InlineKeyboardButton("Готово", callback_data="kb:done")])

        await update.effective_message.reply_text(
            sync_summary + "Выберите документы для контекста (повторное нажатие — снять выбор):",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    # ---------- Сообщения ----------
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        user_text = update.effective_message.text

        await self._send_action(update.effective_chat.id, context, ChatAction.TYPING)

        kb_ctx = None
        if KB_AVAILABLE and self.kb_retriever and self.kb_ctx and st.kb_selected_ids:
            try:
                chunks: List[KBChunk] = await asyncio.to_thread(
                    self.kb_retriever.retrieve, user_text, st.kb_selected_ids,  # k по настройкам внутри
                )
                kb_ctx = self.kb_ctx.build_context(chunks)
            except Exception as e:
                logger.warning("KB retrieve failed: %s", e)

        try:
            reply = await asyncio.to_thread(
                self.openai.chat,
                user_text,
                st.model or getattr(self.settings, "openai_model", None),
                getattr(self.settings, "openai_temperature", 0.2),
                st.style,
                kb_ctx,
            )
            await update.effective_message.reply_text(reply)
        except Exception as e:
            logger.exception("text chat failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка обращения к OpenAI: {e}")

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        file = await update.effective_message.voice.get_file()
        file_bytes = await file.download_as_bytearray()

        await self._send_action(update.effective_chat.id, context, ChatAction.TYPING)
        try:
            transcript = await asyncio.to_thread(self.openai.transcribe_audio, bytes(file_bytes))
        except Exception as e:
            logger.exception("transcribe failed: %s", e)
            await update.effective_message.reply_text(f"Не удалось распознать аудио: {e}")
            return

        kb_ctx = None
        if KB_AVAILABLE and self.kb_retriever and self.kb_ctx and st.kb_selected_ids:
            try:
                chunks: List[KBChunk] = await asyncio.to_thread(
                    self.kb_retriever.retrieve, transcript, st.kb_selected_ids,
                )
                kb_ctx = self.kb_ctx.build_context(chunks)
            except Exception as e:
                logger.warning("KB retrieve failed: %s", e)

        try:
            reply = await asyncio.to_thread(
                self.openai.chat,
                transcript,
                st.model or getattr(self.settings, "openai_model", None),
                getattr(self.settings, "openai_temperature", 0.2),
                st.style,
                kb_ctx,
            )
            await update.effective_message.reply_text(f"🎙️ Вы сказали: {transcript}\n\nОтвет:\n{reply}")
        except Exception as e:
            logger.exception("voice chat failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка обращения к OpenAI: {e}")

    async def on_file_or_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Получение файла/фото — только анализ (описание), без автодобавления в БЗ.
        Добавление в БЗ доступно администратору через /kb (синхронизация).
        """
        message = update.effective_message
        await self._send_action(update.effective_chat.id, context, ChatAction.TYPING)

        try:
            if message.document:
                file = await message.document.get_file()
                content = await file.download_as_bytearray()
                summary = await asyncio.to_thread(self.openai.describe_file, bytes(content), message.document.file_name)
                await message.reply_text(f"📄 Файл: {message.document.file_name}\nАнализ:\n{summary}")
            elif message.photo:
                file = await message.photo[-1].get_file()
                content = await file.download_as_bytearray()
                summary = await asyncio.to_thread(self.openai.describe_image, bytes(content))
                await message.reply_text(f"🖼️ Фото получено.\nАнализ:\n{summary}")
        except Exception as e:
            logger.exception("file/photo analyze failed: %s", e)
            await message.reply_text(f"Не удалось проанализировать вложение: {e}")

    # ---------- Inline callbacks ----------
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return
        await query.answer()

        data = (query.data or "").strip()
        user_id = update.effective_user.id
        st = self._ensure_dialog(user_id)

        # model:<name>
        if data.startswith("model:"):
            name = data.split(":", 1)[1]
            st.model = name
            await query.edit_message_text(f"Модель установлена: {name}")
            return

        # mode:<name>
        if data.startswith("mode:"):
            name = data.split(":", 1)[1]
            st.style = name
            await query.edit_message_text(f"Стиль установлен: {name}")
            return

        # dialogs
        if data == "newdlg":
            dlg_id = self._next_dialog_id
            self._next_dialog_id += 1
            self._dialogs_by_user.setdefault(user_id, {})[dlg_id] = DialogState(dialog_id=dlg_id)
            self._current_dialog_by_user[user_id] = dlg_id
            await query.edit_message_text("Создан новый диалог.")
            return

        if data.startswith("open:"):
            dlg_id = int(data.split(":", 1)[1])
            if dlg_id in self._dialogs_by_user.get(user_id, {}):
                self._current_dialog_by_user[user_id] = dlg_id
                await query.edit_message_text(f"Открыт диалог #{dlg_id}.")
            else:
                await query.edit_message_text("Диалог не найден.")
            return

        if data.startswith("del:"):
            dlg_id = int(data.split(":", 1)[1])
            if dlg_id in self._dialogs_by_user.get(user_id, {}):
                del self._dialogs_by_user[user_id][dlg_id]
                if self._current_dialog_by_user.get(user_id) == dlg_id:
                    rest = list(self._dialogs_by_user.get(user_id, {}).keys())
                    self._current_dialog_by_user[user_id] = rest[0] if rest else None
                await query.edit_message_text(f"Диалог #{dlg_id} удален.")
            else:
                await query.edit_message_text("Диалог не найден.")
            return

        # KB callbacks
        if data == "kb:done":
            await query.edit_message_text("Выбор документов сохранён.")
            return

        if data.startswith("kb:toggle:"):
            doc_id = data.split(":", 2)[2]
            if doc_id in st.kb_selected_ids:
                st.kb_selected_ids.remove(doc_id)
                await query.edit_message_text(f"❎ Документ выключен: {doc_id}")
            else:
                st.kb_selected_ids.append(doc_id)
                await query.edit_message_text(f"✅ Документ добавлен: {doc_id}")
            return

        if data == "kb:none":
            await query.edit_message_text("Документы отсутствуют.")
            return
