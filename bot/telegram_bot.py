# bot/telegram_bot.py
from __future__ import annotations

import asyncio
import io
import logging
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
from telegram.error import Conflict

# ---------- Логирование ----------
logger = logging.getLogger(__name__)

# ---------- База знаний (KB) — безопасный импорт ----------
KB_MOD_AVAILABLE = True
try:
    # Единая точка входа пакета БЗ (см. bot/knowledge_base/__init__.py)
    from bot.knowledge_base import (
        KnowledgeBaseIndexer,
        KnowledgeBaseRetriever,
        KBChunk,
        KBDocument,
    )
except Exception as e:
    KB_MOD_AVAILABLE = False
    logger.warning("KB unavailable (import): %s", e)


# ---------- Состояние диалога ----------
@dataclass
class DialogState:
    dialog_id: int
    title: str = "Диалог"
    created_at_ts: float = 0.0
    updated_at_ts: float = 0.0
    model: Optional[str] = None
    style: str = "Pro"
    kb_selected_docs: List[str] = field(default_factory=list)  # список ext_id выбранных документов


# ======================================================================
#                                БОТ
# ======================================================================
class ChatGPTTelegramBot:
    def __init__(self, openai, settings):
        self.openai = openai
        self.settings = settings

        # Разрешения (опционально)
        self.admin_ids = set(
            getattr(settings, "admin_user_ids", [])
            or getattr(settings, "admin_set", [])
            or []
        )
        self.allowed_ids = set(
            getattr(settings, "allowed_user_ids", [])
            or getattr(settings, "allowed_set", [])
            or []
        )

        # Диалоги на пользователя (in-memory)
        self._dialogs_by_user: Dict[int, Dict[int, DialogState]] = {}
        self._current_dialog_by_user: Dict[int, int] = {}
        self._next_dialog_id: int = 1

        # БЗ
        self.kb_available: bool = KB_MOD_AVAILABLE
        self.kb_indexer: Optional[KnowledgeBaseIndexer] = None
        self.kb_retriever: Optional[KnowledgeBaseRetriever] = None

        if self.kb_available:
            try:
                self.kb_indexer = KnowledgeBaseIndexer(settings)
                self.kb_retriever = KnowledgeBaseRetriever(settings)
            except Exception as e:
                self.kb_available = False
                logger.exception("KB init failed: %s", e)

    # ------------------------------------------------------------------
    #                     Регистрация команд в меню
    # ------------------------------------------------------------------
    def _build_commands(self) -> List[BotCommand]:
        return [
            BotCommand("start", "запуск/меню"),
            BotCommand("help", "помощь"),
            BotCommand("reset", "сброс контекста"),
            BotCommand("stats", "статистика"),
            BotCommand("kb", "база знаний"),
            BotCommand("model", "выбор модели"),
            BotCommand("mode", "стиль ответа"),
            BotCommand("dialogs", "список диалогов"),
            BotCommand("img", "создать изображение"),
            BotCommand("web", "веб-поиск"),
        ]

    async def cmd_kbdebug(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not (self.kb_indexer and self.kb_retriever):
            await update.effective_message.reply_text("KB недоступна в этой сборке.")
            return
        try:
            report = await asyncio.to_thread(self.kb_indexer.diagnose)
            # Телеграм ограничивает длину сообщения ~4096 символов
            max_len = 3900
            if len(report) <= max_len:
                await update.effective_message.reply_text(f"```\n{report}\n```", parse_mode="Markdown")
            else:
                await update.effective_message.reply_text(f"```\n{report[:max_len]}\n... (truncated)\n```",
                                                          parse_mode="Markdown")
        except Exception as e:
            logger.exception("kbdebug failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка диагностики KB: {e}")
    
    async def setup_commands_and_cleanup(self, app: Application) -> None:
        """
        Колбэк для Application.post_init — сначала чистим webhook (на всякий),
        затем устанавливаем команды во всех scope.
        """
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception:
            pass

        # Гарантированно убираем webhook, чтобы не было конфликтов режимов
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            logger.warning("delete_webhook failed: %s", e)

        commands = self._build_commands()
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

    # ------------------------------------------------------------------
    #                      Регистрация обработчиков
    # ------------------------------------------------------------------
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
        app.add_handler(CommandHandler("kbdebug", self.cmd_kbdebug))


        # Сообщения
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file_or_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        # Inline callbacks
        app.add_handler(CallbackQueryHandler(self.on_callback))

    # ------------------------------------------------------------------
    #                           Вспомогательные
    # ------------------------------------------------------------------
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

    async def _send_typing(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass

    def _kb_all_documents(self) -> List[KBDocument]:
        if not (self.kb_available and self.kb_retriever):
            return []
        try:
            return self.kb_retriever.list_documents()
        except Exception as e:
            logger.exception("KB list_documents failed: %s", e)
            return []

    def _prepare_kb_context(self, chunks: List[KBChunk]) -> str:
        if not chunks:
            return ""
        lines: List[str] = []
        lines.append("Ниже даны выдержки из выбранных документов базы знаний (используй их с приоритетом):")
        for i, ch in enumerate(chunks, 1):
            src = ch.source_path or ch.ext_id
            page = f", стр. {ch.page}" if ch.page is not None else ""
            lines.append(f"[{i}] {ch.title}{page} ({src})")
            lines.append(ch.content.strip())
            lines.append("-" * 80)
        return "\n".join(lines)

    def _kb_render_menu(self, st: DialogState) -> InlineKeyboardMarkup:
        docs = self._kb_all_documents()
        rows: List[List[InlineKeyboardButton]] = []
        rows.append([InlineKeyboardButton("🔄 Синхронизировать", callback_data="kb:sync")])

        if not docs:
            rows.append([InlineKeyboardButton("— Документов нет —", callback_data="kb:nop")])
        else:
            for d in docs:
                checked = "✅" if d.ext_id in st.kb_selected_docs else "⬜"
                title = d.title or d.ext_id
                rows.append([
                    InlineKeyboardButton(f"{checked} {title}", callback_data=f"kb:toggle:{d.ext_id}")
                ])

        rows.append([InlineKeyboardButton("✅ Готово", callback_data="kb:close")])
        return InlineKeyboardMarkup(rows)

    # ------------------------------------------------------------------
    #                              Команды
    # ------------------------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_dialog(update.effective_user.id)
        await update.effective_message.reply_text(
            "Привет! Я готов к работе.\n"
            "Команды: /help, /reset, /stats, /kb, /model, /mode, /dialogs, /img, /web"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "/reset — сброс контекста (новый диалог)\n"
            "/stats — статистика\n"
            "/kb — база знаний (синхронизация и выбор документов)\n"
            "/model — выбор модели OpenAI\n"
            "/mode — стиль ответов\n"
            "/dialogs — список диалогов (открыть/удалить)\n"
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
            f"- Документов в контексте: {len(st.kb_selected_docs)}\n"
            f"- Выбрано: {kb_list}"
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
            await update.effective_message.reply_photo(photo=bio, caption=f"🖼️ Сгенерировано по prompt:\n{used_prompt}")
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
        st = self._ensure_dialog(update.effective_user.id)

        if not (self.kb_available and self.kb_indexer and self.kb_retriever):
            await update.effective_message.reply_text("Модуль базы знаний недоступен в этой сборке.")
            return

        # 1) Синхронизация при входе в меню
        try:
            added, updated_cnt, deleted, unchanged = await asyncio.to_thread(self.kb_indexer.sync)
            sync_msg = (
                "Синхронизация БЗ завершена:\n"
                f"• Добавлено: {added}\n• Обновлено: {updated_cnt}\n• Удалено: {deleted}\n• Без изменений: {unchanged}\n\n"
                "Выберите документы для контекста:"
            )
        except Exception as e:
            logger.exception("KB sync failed: %s", e)
            sync_msg = "Не удалось синхронизировать БЗ. Можно выбрать из текущего списка."

        kb_markup = self._kb_render_menu(st)
        await update.effective_message.reply_text(sync_msg, reply_markup=kb_markup)

    # ------------------------------------------------------------------
    #                             Сообщения
    # ------------------------------------------------------------------
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        user_text = update.effective_message.text

        await self._send_typing(update.effective_chat.id, context)

        kb_ctx_text = ""
        if self.kb_available and self.kb_retriever and st.kb_selected_docs:
            try:
                chunks = await asyncio.to_thread(
                    self.kb_retriever.retrieve, user_text, st.kb_selected_docs
                )
                kb_ctx_text = self._prepare_kb_context(chunks)
            except Exception as e:
                logger.warning("KB retrieve failed: %s", e)

        try:
            reply = await asyncio.to_thread(
                self.openai.chat,
                user_text,
                st.model or getattr(self.settings, "openai_model", None),
                getattr(self.settings, "openai_temperature", 0.2),
                st.style,
                kb_ctx_text,
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

        kb_ctx_text = ""
        if self.kb_available and self.kb_retriever and st.kb_selected_docs:
            try:
                chunks = await asyncio.to_thread(
                    self.kb_retriever.retrieve, transcript, st.kb_selected_docs
                )
                kb_ctx_text = self._prepare_kb_context(chunks)
            except Exception as e:
                logger.warning("KB retrieve failed: %s", e)

        try:
            reply = await asyncio.to_thread(
                self.openai.chat,
                transcript,
                st.model or getattr(self.settings, "openai_model", None),
                getattr(self.settings, "openai_temperature", 0.2),
                st.style,
                kb_ctx_text,
            )
            await update.effective_message.reply_text(f"🎙️ Вы сказали: {transcript}\n\nОтвет:\n{reply}")
        except Exception as e:
            logger.exception("voice chat failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка обращения к OpenAI: {e}")

    async def on_file_or_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        При получении файла/фото: анализируем и даём краткое описание.
        Добавление в БЗ делается через /kb (выбор документов), НЕ автоматически.
        """
        message = update.effective_message
        await self._send_typing(update.effective_chat.id, context)

        try:
            if message.document:
                file = await message.document.get_file()
                content = await file.download_as_bytearray()
                summary = await asyncio.to_thread(
                    self.openai.describe_file, bytes(content), message.document.file_name
                )
                await message.reply_text(f"📄 Файл получен: {message.document.file_name}\nАнализ:\n{summary}")
            elif message.photo:
                file = await message.photo[-1].get_file()
                content = await file.download_as_bytearray()
                summary = await asyncio.to_thread(self.openai.describe_image, bytes(content))
                await message.reply_text(f"🖼️ Фото получено. Анализ:\n{summary}")
        except Exception as e:
            logger.exception("file/photo analyze failed: %s", e)
            await message.reply_text(f"Не удалось проанализировать вложение: {e}")

    # ------------------------------------------------------------------
    #                          Inline callbacks
    # ------------------------------------------------------------------
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return
        await query.answer()

        data = query.data or ""
        user_id = update.effective_user.id
        st = self._ensure_dialog(user_id)

        # --------- model:<name> ---------
        if data.startswith("model:"):
            name = data.split(":", 1)[1]
            st.model = name
            await query.edit_message_text(f"Модель установлена: {name}")
            return

        # --------- mode:<name> ----------
        if data.startswith("mode:"):
            name = data.split(":", 1)[1]
            st.style = name
            await query.edit_message_text(f"Стиль установлен: {name}")
            return

        # --------- dialogs --------------
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

        # --------- KB -------------------
        if data == "kb:sync":
            if not (self.kb_available and self.kb_indexer and self.kb_retriever):
                await query.edit_message_text("Модуль базы знаний недоступен.")
                return
            try:
                added, updated_cnt, deleted, unchanged = await asyncio.to_thread(self.kb_indexer.sync)
                prefix = (
                    "Синхронизация завершена:\n"
                    f"• Добавлено: {added}\n• Обновлено: {updated_cnt}\n• Удалено: {deleted}\n• Без изменений: {unchanged}\n\n"
                    "Выберите документы:"
                )
            except Exception as e:
                logger.exception("KB sync failed: %s", e)
                prefix = "Не удалось синхронизировать. Выберите документы из текущего списка."
            await query.edit_message_text(prefix)
            await query.message.reply_text("Документы:", reply_markup=self._kb_render_menu(st))
            return

        if data.startswith("kb:toggle:"):
            if not self.kb_available:
                await query.edit_message_text("Модуль базы знаний недоступен.")
                return
            ext_id = data.split(":", 2)[2]
            if ext_id in st.kb_selected_docs:
                st.kb_selected_docs.remove(ext_id)
            else:
                st.kb_selected_docs.append(ext_id)
            try:
                await query.edit_message_reply_markup(reply_markup=self._kb_render_menu(st))
            except Exception:
                await query.message.reply_text("Документы:", reply_markup=self._kb_render_menu(st))
            return

        if data == "kb:close":
            await query.edit_message_text(
                f"Готово. Выбрано документов: {len(st.kb_selected_docs)}."
            )
            return

        if data == "kb:nop":
            return

        await query.edit_message_text("Неизвестное действие.")

    # ------------------------------------------------------------------
    #                        Error handler (важно!)
    # ------------------------------------------------------------------
    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if isinstance(err, Conflict):
            logger.error(
                "Conflict: another getUpdates request is running. "
                "Проверьте, что бот не запущен вторым процессом."
            )
            # Мягко остановим приложение, чтобы избежать спама retry
            try:
                await context.application.stop()
            except Exception:
                pass
            return

        logger.exception("Unhandled error: %s", err)
