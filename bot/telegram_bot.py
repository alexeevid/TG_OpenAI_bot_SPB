# bot/telegram_bot.py
from __future__ import annotations

import asyncio
import io
import logging
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

# --- Опциональная База Знаний (KB) ---
KB_AVAILABLE = True
try:
    from bot.knowledge_base.indexer import KnowledgeBaseIndexer
    from bot.knowledge_base.retriever import KnowledgeBaseRetriever, IndexBuilder
    from bot.knowledge_base.context_manager import ContextManager
except Exception as e:
    KB_AVAILABLE = False
    logger.warning("KB unavailable: %s", e)


# --- Простая модель диалогов в памяти ---
@dataclass
class DialogState:
    dialog_id: int
    title: str = "Диалог"
    created_at_ts: float = 0.0
    updated_at_ts: float = 0.0
    model: Optional[str] = None
    style: str = "Pro"
    # БЗ:
    kb_selected_docs: List[str] = field(default_factory=list)  # выбранные пользователем документы
    kb_strict: bool = True                                    # STRICT: только из БЗ, HYBRID: KB-first
    kb_focus_doc: Optional[str] = None                        # если задан — разговор по конкретной книге


class ChatGPTTelegramBot:
    def __init__(self, openai, settings):
        self.openai = openai
        self.settings = settings

        # Разрешения
        self.admin_ids = set(getattr(settings, "admin_user_ids", []) or getattr(settings, "admin_set", []) or [])
        self.allowed_ids = set(getattr(settings, "allowed_user_ids", []) or getattr(settings, "allowed_set", []) or [])

        # Диалоги
        self._dialogs_by_user: Dict[int, Dict[int, DialogState]] = {}
        self._current_dialog_by_user: Dict[int, int] = {}
        self._next_dialog_id: int = 1

        # KB
        self.kb_indexer: Optional[KnowledgeBaseIndexer] = None
        self.kb_retriever: Optional[KnowledgeBaseRetriever] = None
        self.kb_ctx: Optional[ContextManager] = None
        self.kb_builder: Optional[IndexBuilder] = None
        if KB_AVAILABLE:
            try:
                self.kb_indexer = KnowledgeBaseIndexer(settings)
                self.kb_retriever = KnowledgeBaseRetriever(settings)
                self.kb_retriever.ensure_index()  # подгрузим on-disk, если есть
                self.kb_ctx = ContextManager()
                self.kb_builder = IndexBuilder(settings, self.kb_retriever)
            except Exception as e:
                logger.exception("KB init failed: %s", e)

    # ========== Команды/меню ==========
    def _build_commands(self) -> List[BotCommand]:
        return [
            BotCommand("help", "помощь"),
            BotCommand("reset", "сброс контекста"),
            BotCommand("stats", "статистика"),
            BotCommand("kb", "база знаний"),
            BotCommand("model", "выбор модели OpenAI"),
            BotCommand("mode", "стиль ответов"),
            BotCommand("dialogs", "список диалогов"),
            BotCommand("img", "сгенерировать изображение"),
            BotCommand("web", "веб-поиск"),
        ]

    async def setup_commands(self, app: Application):
        """post_init callback (Application.builder().post_init(self.setup_commands))"""
        commands = self._build_commands()
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

    # ========== Регистрация обработчиков ==========
    def install(self, app: Application) -> None:
        # Команды
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("reset", self.cmd_reset))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("dialogs", self.cmd_dialogs))
        app.add_handler(CommandHandler("img", self.cmd_img))
        app.add_handler(CommandHandler("web", self.cmd_web))
        # KB
        app.add_handler(CommandHandler("kb", self.cmd_kb))

        # Сообщения пользователя
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file_or_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        # Inline callbacks
        app.add_handler(CallbackQueryHandler(self.on_callback))

    # ========== Вспомогательные ==========
    def _ensure_dialog(self, user_id: int) -> DialogState:
        user_dialogs = self._dialogs_by_user.setdefault(user_id, {})
        if user_id not in self._current_dialog_by_user or self._current_dialog_by_user[user_id] not in user_dialogs:
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

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids if self.admin_ids else True  # если не задано — считаем, что все админы

    # ========== Команды ==========
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_dialog(update.effective_user.id)
        await update.effective_message.reply_text(
            "Привет! Я готов к работе.\nКоманды: /help, /reset, /stats, /kb, /model, /mode, /dialogs, /img, /web"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "/reset — сброс контекста (новый диалог)\n"
            "/stats — статистика\n"
            "/kb — база знаний (синхронизация/выбор/режим)\n"
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
            f"- Режим БЗ: {'STRICT' if st.kb_strict else 'HYBRID'}\n"
            f"- Выбрано документов: {len(st.kb_selected_docs)}\n"
            f"- В контексте: {kb_list}"
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
        user_id = update.effective_user.id
        st = self._ensure_dialog(user_id)

        if not KB_AVAILABLE or not (self.kb_indexer and self.kb_retriever and self.kb_ctx and self.kb_builder):
            await update.effective_message.reply_text("Модуль базы знаний недоступен в этой сборке.")
            return

        # 1) если админ — запускаем синхронизацию прямо из команды
        new_badge = ""
        if self._is_admin(user_id):
            try:
                added, updated, deleted, unchanged = await asyncio.to_thread(self.kb_indexer.sync)
                if (added or updated):
                    new_badge = f" (новые/обновлённые: {added + updated})"
            except Exception as e:
                logger.exception("KB sync failed: %s", e)
                await update.effective_message.reply_text(f"Ошибка синхронизации: {e}")
                return

        # 2) показываем список документов
        docs = [d.__dict__ for d in self.kb_indexer.list_all()]
        if not docs:
            await update.effective_message.reply_text("В базе знаний пока нет документов.")
            return

        rows: List[List[InlineKeyboardButton]] = []
        # Верхняя панель: переключатели режима
        rows.append([InlineKeyboardButton(
            f"Режим: {'STRICT' if st.kb_strict else 'HYBRID'}", callback_data=f"kb:strict:{0 if st.kb_strict else 1}"
        )])
        # Фокус
        if st.kb_focus_doc:
            rows.append([InlineKeyboardButton("🎯 Сбросить фокус", callback_data="kb:unfocus")])

        # Список документов с чекбоксами
        for d in docs:
            doc_id = d["doc_id"]
            title = d.get("title") or doc_id
            mark = "✅" if doc_id in st.kb_selected_docs else "⬜"
            if st.kb_focus_doc == doc_id:
                title = f"[F] {title}"
            rows.append([
                InlineKeyboardButton(f"{mark} {title}", callback_data=f"kb:pick:{doc_id}"),
                InlineKeyboardButton("🎯", callback_data=f"kb:focus:{doc_id}"),
            ])

        header = "База знаний" + new_badge
        await update.effective_message.reply_text(header, reply_markup=InlineKeyboardMarkup(rows))

    # ========== Сообщения ==========
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        user_text = update.effective_message.text

        await self._send_typing(update.effective_chat.id, context)

        kb_ctx_text = None
        chunks = []
        if KB_AVAILABLE and self.kb_retriever and self.kb_ctx and st.kb_selected_docs:
            doc_scope = [st.kb_focus_doc] if st.kb_focus_doc else list(st.kb_selected_docs)
            try:
                chunks = await asyncio.to_thread(
                    self.kb_retriever.retrieve, user_text, doc_scope, self.openai.embed_texts, 8
                )
                kb_ctx_text = self.kb_ctx.build_context(chunks) if chunks else None
            except Exception as e:
                logger.warning("KB retrieve failed: %s", e)

        # strict поведение: если включён строгий режим и нет контекста — честный ответ
        if st.kb_selected_docs and st.kb_strict and not kb_ctx_text:
            await update.effective_message.reply_text(
                "В выбранных документах базы знаний не найдено релевантной информации к запросу."
            )
            return

        try:
            reply = await asyncio.to_thread(
                self.openai.chat,
                user_text,
                st.model or getattr(self.settings, "openai_model", None),
                getattr(self.settings, "openai_temperature", 0.2),
                st.style,
                kb_ctx_text,
                st.kb_selected_docs and st.kb_strict,
            )
            footer = self.kb_ctx.build_sources_footer(chunks) if chunks else ""
            await update.effective_message.reply_text(reply + (footer if footer else ""))
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

        kb_ctx_text = None
        chunks = []
        if KB_AVAILABLE and self.kb_retriever and self.kb_ctx and st.kb_selected_docs:
            doc_scope = [st.kb_focus_doc] if st.kb_focus_doc else list(st.kb_selected_docs)
            try:
                chunks = await asyncio.to_thread(
                    self.kb_retriever.retrieve, transcript, doc_scope, self.openai.embed_texts, 8
                )
                kb_ctx_text = self.kb_ctx.build_context(chunks) if chunks else None
            except Exception as e:
                logger.warning("KB retrieve failed: %s", e)

        if st.kb_selected_docs and st.kb_strict and not kb_ctx_text:
            await update.effective_message.reply_text(
                f"🎙️ Вы сказали: {transcript}\n\n"
                "В выбранных документах базы знаний не найдено релевантной информации к запросу."
            )
            return

        try:
            reply = await asyncio.to_thread(
                self.openai.chat,
                transcript,
                st.model or getattr(self.settings, "openai_model", None),
                getattr(self.settings, "openai_temperature", 0.2),
                st.style,
                kb_ctx_text,
                st.kb_selected_docs and st.kb_strict,
            )
            footer = self.kb_ctx.build_sources_footer(chunks) if chunks else ""
            await update.effective_message.reply_text(f"🎙️ Вы сказали: {transcript}\n\nОтвет:\n{reply}" + (footer if footer else ""))
        except Exception as e:
            logger.exception("voice chat failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка обращения к OpenAI: {e}")

    async def on_file_or_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Получение файла/фото — только анализ (без автозагрузки в БЗ).
        """
        message = update.effective_message
        await self._send_typing(update.effective_chat.id, context)

        try:
            if message.document:
                file = await message.document.get_file()
                content = await file.download_as_bytearray()
                summary = await asyncio.to_thread(self._analyze_doc, bytes(content), message.document.file_name)
                await message.reply_text(f"📄 Файл получен: {message.document.file_name}\nАнализ:\n{summary}")
            elif message.photo:
                file = await message.photo[-1].get_file()
                content = await file.download_as_bytearray()
                summary = await asyncio.to_thread(self._analyze_img, bytes(content))
                await message.reply_text(f"🖼️ Фото получено. Анализ:\n{summary}")
        except Exception as e:
            logger.exception("file/photo analyze failed: %s", e)
            await message.reply_text(f"Не удалось проанализировать вложение: {e}")

    def _analyze_doc(self, content: bytes, name: str) -> str:
        # минимальная заглушка (можно сделать лучше через vision или text scanner)
        prompt = f"Кратко опиши, что за документ '{name}', какие ключевые темы и как его лучше использовать."
        return self.openai.chat(prompt, model="gpt-4o-mini", temperature=0.2, style="Pro")

    def _analyze_img(self, content: bytes) -> str:
        prompt = "Опиши изображение и возможные полезные выводы для делового контекста."
        return self.openai.chat(prompt, model="gpt-4o-mini", temperature=0.2, style="Pro")

    # ========== Inline callbacks ==========
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

        # -------------- KB callbacks --------------
        if data.startswith("kb:strict:"):
            v = int(data.split(":", 2)[2])
            st.kb_strict = bool(v)
            await query.edit_message_text(f"Режим БЗ: {'STRICT' if st.kb_strict else 'HYBRID'}")
            return

        if data == "kb:unfocus":
            st.kb_focus_doc = None
            await query.edit_message_text("Фокус на книге снят.")
            return

        if data.startswith("kb:focus:"):
            doc_id = data.split(":", 2)[2]
            st.kb_focus_doc = doc_id
            if doc_id not in st.kb_selected_docs:
                st.kb_selected_docs.append(doc_id)
            await query.edit_message_text("Фокус установлен.")
            return

        if data.startswith("kb:pick:"):
            doc_id = data.split(":", 2)[2]
            if doc_id in st.kb_selected_docs:
                st.kb_selected_docs.remove(doc_id)
                if st.kb_focus_doc == doc_id:
                    st.kb_focus_doc = None
            else:
                st.kb_selected_docs.append(doc_id)
            await query.edit_message_text("Список документов обновлён.")
            return
