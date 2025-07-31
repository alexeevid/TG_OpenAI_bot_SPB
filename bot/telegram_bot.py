# bot/telegram_bot.py
from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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

# ===== Попытка импортировать модуль БЗ (Knowledge Base) безопасно =====
_KB_IMPORT_OK = False
_KB_IMPORT_ERR: Optional[Exception] = None
try:
    from bot.knowledge_base.indexer import KnowledgeBaseIndexer  # type: ignore
    from bot.knowledge_base.retriever import KnowledgeBaseRetriever  # type: ignore
    from bot.knowledge_base.context_manager import ContextManager  # type: ignore
    _KB_IMPORT_OK = True
except Exception as e:
    _KB_IMPORT_OK = False
    _KB_IMPORT_ERR = e
    logger.warning("KB unavailable (import): %s", e)


# ===== Наивная in-memory модель диалога =====
@dataclass
class DialogState:
    dialog_id: int
    title: str = "Диалог"
    created_at_ts: float = field(default_factory=lambda: time.time())
    updated_at_ts: float = field(default_factory=lambda: time.time())
    model: Optional[str] = None
    style: str = "Pro"
    kb_selected_docs: List[str] = field(default_factory=list)  # список выбранных документов для контекста


class ChatGPTTelegramBot:
    def __init__(self, openai, settings):
        self.openai = openai
        self.settings = settings

        # Разрешения
        self.admin_ids = set(getattr(settings, "admin_user_ids", []) or getattr(settings, "admin_set", []) or [])
        self.allowed_ids = set(getattr(settings, "allowed_user_ids", []) or getattr(settings, "allowed_set", []) or [])

        # Состояние пользователей (если есть БД — можно перенести туда)
        self._dialogs_by_user: Dict[int, Dict[int, DialogState]] = {}
        self._current_dialog_by_user: Dict[int, int] = {}
        self._next_dialog_id: int = 1

        # Состояние KB на уровне экземпляра
        self.kb_available: bool = _KB_IMPORT_OK
        self.kb_import_error: Optional[str] = str(_KB_IMPORT_ERR) if _KB_IMPORT_ERR else None
        self.kb_indexer: Optional[KnowledgeBaseIndexer] = None
        self.kb_retriever: Optional[KnowledgeBaseRetriever] = None
        self.kb_ctx: Optional[ContextManager] = None

        if self.kb_available:
            try:
                self.kb_indexer = KnowledgeBaseIndexer(settings)
                self.kb_retriever = KnowledgeBaseRetriever(settings)
                self.kb_ctx = ContextManager(settings)
            except Exception as e:
                logger.exception("KB init failed: %s", e)
                self.kb_available = False
                self.kb_import_error = f"KB init failed: {e}"

    # ========= Команды/меню =========
    def _build_commands(self) -> List[BotCommand]:
        return [
            BotCommand("help", "помощь"),
            BotCommand("reset", "сброс контекста"),
            BotCommand("stats", "статистика"),
            BotCommand("kb", "база знаний"),
            BotCommand("model", "выбор модели"),
            BotCommand("mode", "стиль ответов"),
            BotCommand("dialogs", "диалоги (открыть/удалить)"),
            BotCommand("img", "сгенерировать изображение"),
            BotCommand("web", "веб-поиск"),
        ]

    async def setup_commands(self, app: Application) -> None:
        """
        Этот метод мы передаем в .post_init(builder), он вызывается один раз после инициализации Application.
        """
        commands = self._build_commands()

        # кнопка меню
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception:
            pass

        scopes = [
            BotCommandScopeDefault(),
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllChatAdministrators(),
        ]
        for scope in scopes:
            try:
                await app.bot.delete_my_commands(scope=scope)
            except Exception:
                pass
            await app.bot.set_my_commands(commands=commands, scope=scope)

        logger.info("✅ Команды установлены (global scopes)")

    # ========= Регистрация обработчиков =========
    def install(self, app: Application) -> None:
        # Команды
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("reset", self.cmd_reset))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("kb", self.cmd_kb))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("dialogs", self.cmd_dialogs))
        app.add_handler(CommandHandler("img", self.cmd_img))
        app.add_handler(CommandHandler("web", self.cmd_web))

        # Сообщения пользователя
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file_or_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        # Inline callbacks
        app.add_handler(CallbackQueryHandler(self.on_callback))

    # ========= Вспомогательные =========
    def _ensure_dialog(self, user_id: int) -> DialogState:
        self._dialogs_by_user.setdefault(user_id, {})
        if user_id not in self._current_dialog_by_user:
            dlg_id = self._next_dialog_id
            self._next_dialog_id += 1
            st = DialogState(dialog_id=dlg_id)
            self._dialogs_by_user[user_id][dlg_id] = st
            self._current_dialog_by_user[user_id] = dlg_id
        dlg = self._dialogs_by_user[user_id][self._current_dialog_by_user[user_id]]
        dlg.updated_at_ts = time.time()
        return dlg

    def _list_dialogs(self, user_id: int) -> List[DialogState]:
        return list(self._dialogs_by_user.get(user_id, {}).values())

    async def _send_typing(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass

    # ========= Команды =========
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_dialog(update.effective_user.id)
        await update.effective_message.reply_text(
            "Привет! Я готов к работе.\nКоманды: /help, /reset, /stats, /kb, /model, /mode, /dialogs, /img, /web"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "/reset — сброс контекста (новый диалог)\n"
            "/stats — статс текущего диалога\n"
            "/kb — база знаний: синхрон/выбор документов\n"
            "/model — выбор модели\n"
            "/mode — стиль ответов\n"
            "/dialogs — список диалогов (открыть/удалить/создать)\n"
            "/img — сгенерировать изображение\n"
            "/web — веб-поиск\n"
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
        kb_list = ", ".join(st.kb_selected_docs) if st.kb_selected_docs else "—"
        text = (
            "📊 Статистика:\n"
            f"- Диалог: {st.title}\n"
            f"- Модель: {st.model or getattr(self.settings, 'openai_model', 'gpt-4o')}\n"
            f"- Стиль: {st.style}\n"
            f"- Документов выбрано: {len(st.kb_selected_docs)}\n"
            f"- В контексте: {kb_list}\n"
            f"- БЗ: {'доступна' if self.kb_available else 'недоступна'}"
        )
        await update.effective_message.reply_text(text)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        try:
            models = self.openai.list_models_for_menu()  # ожидается список строк
        except Exception as e:
            logger.exception("list_models failed: %s", e)
            await update.effective_message.reply_text("Не удалось получить список моделей.")
            return

        rows = []
        current = st.model or getattr(self.settings, "openai_model", None)
        for name in models:
            mark = "✅ " if name == current else ""
            rows.append([InlineKeyboardButton(f"{mark}{name}", callback_data=f"model:{name}")])
        kb = InlineKeyboardMarkup(rows)
        await update.effective_message.reply_text("Выберите модель:", reply_markup=kb)

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
        await self._send_typing(update.effective_chat.id, context)
        try:
            img_bytes, used_prompt = await asyncio.to_thread(self.openai.generate_image, prompt, None)
            bio = io.BytesIO(img_bytes)
            bio.name = "image.png"
            await update.effective_message.reply_photo(
                photo=InputFile(bio),
                caption=f"🖼️ Сгенерировано по prompt:\n{used_prompt}",
            )
        except Exception as e:
            logger.exception("Image generation failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка генерации изображения: {e}")

    async def cmd_web(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.effective_message.reply_text("Использование: /web <запрос>")
            return
        query = " ".join(context.args)
        await self._send_typing(update.effective_chat.id, context)
        try:
            answer, sources = await asyncio.to_thread(self.openai.web_answer, query)
            if sources:
                src_text = "\n\nИсточники:\n" + "\n".join(f"• {u}" for u in sources)
            else:
                src_text = "\n\n⚠️ Модель не вернула явных ссылок-источников."
            await update.effective_message.reply_text(answer + src_text)
        except Exception as e:
            logger.exception("Web search failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка веб-поиска: {e}")

    async def cmd_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.kb_available or not (self.kb_indexer and self.kb_retriever and self.kb_ctx):
            msg = "Модуль базы знаний недоступен в этой сборке."
            if self.kb_import_error:
                msg += f"\nПричина: {self.kb_import_error}"
            await update.effective_message.reply_text(msg)
            return

        st = self._ensure_dialog(update.effective_user.id)

        # Кнопки KB: синхронизация + выбор документов
        rows = [
            [InlineKeyboardButton("🔄 Синхронизировать", callback_data="kb:sync")],
            [InlineKeyboardButton("📄 Выбрать документы", callback_data="kb:pick")],
        ]
        if st.kb_selected_docs:
            rows.append([InlineKeyboardButton("🧹 Очистить выбор", callback_data="kb:clear")])

        await update.effective_message.reply_text("База знаний:", reply_markup=InlineKeyboardMarkup(rows))

    # ========= Сообщения (текст/голос/файлы/фото) =========
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        user_text = update.effective_message.text

        await self._send_typing(update.effective_chat.id, context)

        kb_ctx = None
        if self.kb_available and st.kb_selected_docs and self.kb_retriever and self.kb_ctx:
            try:
                chunks = await asyncio.to_thread(self.kb_retriever.retrieve, user_text, st.kb_selected_docs)
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

        await self._send_typing(update.effective_chat.id, context)
        try:
            transcript = await asyncio.to_thread(self.openai.transcribe_audio, bytes(file_bytes))
        except Exception as e:
            logger.exception("transcribe failed: %s", e)
            await update.effective_message.reply_text(f"Не удалось распознать аудио: {e}")
            return

        kb_ctx = None
        if self.kb_available and st.kb_selected_docs and self.kb_retriever and self.kb_ctx:
            try:
                chunks = await asyncio.to_thread(self.kb_retriever.retrieve, transcript, st.kb_selected_docs)
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
        Получая файл/фото бот только анализирует содержимое.
        Добавление в БЗ делается через /kb -> Выбрать документы (не автоматически).
        """
        message = update.effective_message
        await self._send_typing(update.effective_chat.id, context)

        try:
            if message.document:
                file = await message.document.get_file()
                content = await file.download_as_bytearray()
                summary = await asyncio.to_thread(self.openai.describe_file, bytes(content), message.document.file_name)
                await message.reply_text(f"📄 Файл получен: {message.document.file_name}\nАнализ:\n{summary}")
            elif message.photo:
                file = await message.photo[-1].get_file()
                content = await file.download_as_bytearray()
                summary = await asyncio.to_thread(self.openai.describe_image, bytes(content))
                await message.reply_text(f"🖼️ Фото получено. Анализ:\n{summary}")
        except Exception as e:
            logger.exception("file/photo analyze failed: %s", e)
            await message.reply_text(f"Не удалось проанализировать вложение: {e}")

    # ========= Inline callbacks =========
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return
        await query.answer()

        data = query.data or ""
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

        # KB
        if data == "kb:sync":
            if not self.kb_available or not self.kb_indexer:
                await query.edit_message_text("Модуль базы знаний недоступен.")
                return
            try:
                added, updated, deleted, unchanged = await asyncio.to_thread(self.kb_indexer.sync)
                await query.edit_message_text(
                    "Синхронизация завершена:\n"
                    f"• Добавлено: {added}\n"
                    f"• Обновлено: {updated}\n"
                    f"• Удалено: {deleted}\n"
                    f"• Без изменений: {unchanged}"
                )
            except Exception as e:
                logger.exception("KB sync failed: %s", e)
                await query.edit_message_text(f"Ошибка синхронизации: {e}")
            return

        if data == "kb:pick":
            if not self.kb_available or not (self.kb_retriever and self.kb_indexer):
                await query.edit_message_text("Модуль базы знаний недоступен.")
                return
            # Предполагаем, что indexer/retriever предоставляют API для получения списка документов
            try:
                docs = await asyncio.to_thread(self.kb_retriever.list_documents)  # type: ignore[attr-defined]
            except AttributeError:
                await query.edit_message_text("Список документов недоступен в этой сборке KB.")
                return
            except Exception as e:
                await query.edit_message_text(f"Не удалось получить список документов: {e}")
                return

            # Формируем клавиатуру выбора (toggle)
            rows: List[List[InlineKeyboardButton]] = []
            selected = set(st.kb_selected_docs)
            for doc in docs:
                name = str(doc)
                mark = "✅ " if name in selected else ""
                rows.append([InlineKeyboardButton(f"{mark}{name}", callback_data=f"kb:toggle_doc:{name}")])
            if docs:
                rows.append([InlineKeyboardButton("Готово", callback_data="kb:done")])
            await query.edit_message_text("Выберите документы:", reply_markup=InlineKeyboardMarkup(rows))
            return

        if data == "kb:clear":
            st.kb_selected_docs = []
            await query.edit_message_text("Выбор документов очищен.")
            return

        if data.startswith("kb:toggle_doc:"):
            name = data.split(":", 2)[2]
            if name in st.kb_selected_docs:
                st.kb_selected_docs.remove(name)
            else:
                st.kb_selected_docs.append(name)
            # Обновим отметки
            try:
                docs = await asyncio.to_thread(self.kb_retriever.list_documents)  # type: ignore[attr-defined]
            except Exception:
                docs = st.kb_selected_docs  # fallback
            rows: List[List[InlineKeyboardButton]] = []
            selected = set(st.kb_selected_docs)
            for doc in docs:
                dname = str(doc)
                mark = "✅ " if dname in selected else ""
                rows.append([InlineKeyboardButton(f"{mark}{dname}", callback_data=f"kb:toggle_doc:{dname}")])
            if docs:
                rows.append([InlineKeyboardButton("Готово", callback_data="kb:done")])
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
            return

        if data == "kb:done":
            await query.edit_message_text(
                "Выбор завершён. В контексте:\n" + ("\n".join(f"• {n}" for n in st.kb_selected_docs) or "—")
            )
            return
