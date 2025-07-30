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

# --- Опциональная База Знаний (KB) ---
KB_AVAILABLE = True
try:
    from bot.knowledge_base.indexer import KnowledgeBaseIndexer
    from bot.knowledge_base.retriever import KnowledgeBaseRetriever
    from bot.knowledge_base.context_manager import ContextManager
    from bot.knowledge_base.service import KBService, KBDocument, KBSyncResult
except Exception as e:
    KB_AVAILABLE = False
    logger.warning("KB unavailable: %s", e)

# --- Простейшее состояние диалога (in-memory) ---
@dataclass
class DialogState:
    dialog_id: int
    title: str = "Диалог"
    created_at_ts: float = field(default_factory=lambda: time.time())
    updated_at_ts: float = field(default_factory=lambda: time.time())
    model: Optional[str] = None
    style: str = "Pro"
    # БЗ - список выбранных документов. Пусто => RAG не используется
    kb_selected_docs: List[int | str] = field(default_factory=list)

class ChatGPTTelegramBot:
    def __init__(self, openai, settings):
        self.openai = openai
        self.settings = settings

        # Разрешения
        self.admin_ids = set(getattr(settings, "admin_user_ids", []) or getattr(settings, "admin_set", []) or [])
        self.allowed_ids = set(getattr(settings, "allowed_user_ids", []) or getattr(settings, "allowed_set", []) or [])

        # Состояние пользователей (наивная in‑memory реализация)
        self._dialogs_by_user: Dict[int, Dict[int, DialogState]] = {}
        self._current_dialog_by_user: Dict[int, int] = {}
        self._next_dialog_id: int = 1

        # БЗ
        self.kb_service: Optional[KBService] = None
        if KB_AVAILABLE:
            try:
                indexer = KnowledgeBaseIndexer(settings)
                retriever = KnowledgeBaseRetriever(settings)
                ctx_mgr = ContextManager(settings)
                self.kb_service = KBService(indexer, retriever, ctx_mgr)
            except Exception as e:
                logger.exception("KB init failed: %s", e)

        # Пул «новых» документов (подсветка 🆕 на 60 сек. после синка)
        self._kb_last_added: Dict[int | str, float] = {}

        # Ожидание пароля для encrypted документов: user_id -> {doc_id, tries}
        self._kb_pwd_wait: Dict[int, Dict[str, Any]] = {}

    # ========== Команды/меню ==========
    def _build_commands(self) -> List[BotCommand]:
        # Набор команд, который должен быть у всех (мы удалим старые лишние в post_init)
        return [
            BotCommand("start", "запуск и меню"),
            BotCommand("help", "помощь"),
            BotCommand("reset", "сброс контекста"),
            BotCommand("stats", "статистика"),
            BotCommand("kb", "база знаний (выбор документов)"),
            BotCommand("model", "выбор модели OpenAI"),
            BotCommand("mode", "стиль ответов"),
            BotCommand("dialogs", "список диалогов"),
            BotCommand("img", "сгенерировать изображение"),
            BotCommand("web", "веб‑поиск"),
        ]

    async def setup_commands(self, app: Application):
        """
        Вешаем команды в глобальные scope. Вызывается из Application.builder().post_init(...)
        """
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
        # Сначала сносим старые «зависшие» команды в каждом scope
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

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    # ========== Рендер KB ==========
    def _kb_is_new(self, doc_id: int | str) -> bool:
        ts = self._kb_last_added.get(doc_id)
        return bool(ts and (time.time() - ts) < 60.0)

    def _kb_render_page(self, docs: List[KBDocument], page: int, total_pages: int, st: DialogState) -> InlineKeyboardMarkup:
        selected = set(st.kb_selected_docs)
        rows: List[List[InlineKeyboardButton]] = []
        for d in docs:
            check = "☑" if d.id in selected else "☐"
            lock = " 🔐" if d.encrypted else ""
            new = " 🆕" if self._kb_is_new(d.id) else ""
            rows.append([
                InlineKeyboardButton(f"{check} {d.title}{lock}{new}", callback_data=f"kb:toggle:{d.id}")
            ])
        nav: List[InlineKeyboardButton] = []
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"kb:page:{page-1}"))
        nav.append(InlineKeyboardButton(f"Стр. {page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"kb:page:{page+1}"))
        if nav:
            rows.append(nav)
        # действия
        rows.append([InlineKeyboardButton("🧹 Очистить выбор", callback_data="kb:clear"),
                     InlineKeyboardButton("📄 В контексте", callback_data="kb:selected")])
        return InlineKeyboardMarkup(rows)

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
            "/kb — выбор документов из БЗ (RAG включается, когда выбран хоть один документ)\n"
            "/model — выбор модели OpenAI\n"
            "/mode — стиль ответов\n"
            "/dialogs — список диалогов (открыть/удалить)\n"
            "/img — сгенерировать изображение\n"
            "/web — веб‑поиск\n"
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
        kb_list = ", ".join(map(str, st.kb_selected_docs)) if st.kb_selected_docs else "—"
        text = (
            "📊 Статистика:\n"
            f"- Диалог: {st.title}\n"
            f"- Модель: {st.model or getattr(self.settings, 'openai_model', 'gpt-4o')}\n"
            f"- Стиль: {st.style}\n"
            f"- Документов в контексте: {len(st.kb_selected_docs)}\n"
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
            await update.effective_message.reply_photo(
                photo=InputFile(io.BytesIO(img_bytes), filename="image.png"),
                caption=f"🖼️ Сгенерировано по prompt:\n{used_prompt}"
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
            await update.effective_message.reply_text(f"Ошибка веб‑поиска: {e}")

    async def cmd_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        st = self._ensure_dialog(user_id)

        if not self.kb_service:
            await update.effective_message.reply_text("Модуль базы знаний недоступен в этой сборке.")
            return

        # Админ — запускаем синхронизацию
        if self._is_admin(user_id):
            msg = await update.effective_message.reply_text("⏳ Синхронизирую базу знаний…")
            try:
                res = await asyncio.to_thread(self.kb_service.sync)
                # запоминаем «новые»
                now = time.time()
                for doc_id in res.added_ids:
                    self._kb_last_added[doc_id] = now
                header = (
                    "Синхронизация завершена:\n"
                    f"• Добавлено: {res.added}\n"
                    f"• Обновлено: {res.updated}\n"
                    f"• Удалено: {res.deleted}\n"
                    f"• Без изменений: {res.unchanged}"
                )
            except Exception as e:
                logger.exception("KB sync failed: %s", e)
                await msg.edit_text(f"Ошибка синхронизации: {e}")
                return

            # Показать список документов (страница 1)
            try:
                docs, total_pages = await asyncio.to_thread(self.kb_service.list_docs, 1, 10, None)
            except Exception as e:
                await msg.edit_text(header + f"\n\nНе удалось получить список документов: {e}")
                return
            await msg.edit_text(
                header + "\n\nВыберите документы для контекста:",
                reply_markup=self._kb_render_page(docs, 1, total_pages, st)
            )
        else:
            # Не админ — просто список
            try:
                docs, total_pages = await asyncio.to_thread(self.kb_service.list_docs, 1, 10, None)
            except Exception as e:
                await update.effective_message.reply_text(f"Не удалось получить список документов: {e}")
                return
            await update.effective_message.reply_text(
                "Выберите документы для контекста:",
                reply_markup=self._kb_render_page(docs, 1, total_pages, st)
            )

    # ========== Сообщения ==========
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        st = self._ensure_dialog(user_id)
        user_text = update.effective_message.text

        # Если ждём пароль для зашифрованного документа – перехватываем
        if user_id in self._kb_pwd_wait:
            info = self._kb_pwd_wait[user_id]
            doc_id = info["doc_id"]
            tries = info.get("tries", 0)
            try:
                ok = await asyncio.to_thread(self.kb_service.index_encrypted_with_password, doc_id, user_text)
            except Exception as e:
                ok = False
            if ok:
                # добавляем документ в выбор
                if doc_id not in st.kb_selected_docs:
                    st.kb_selected_docs.append(doc_id)
                del self._kb_pwd_wait[user_id]
                await update.effective_message.reply_text("🔓 Документ разблокирован и добавлен в контекст.")
            else:
                tries += 1
                if tries >= 3:
                    del self._kb_pwd_wait[user_id]
                    await update.effective_message.reply_text("❌ Неверный пароль. Отмена.")
                else:
                    info["tries"] = tries
                    await update.effective_message.reply_text(f"Пароль неверен. Осталось попыток: {3-tries}")
            return

        await self._send_typing(update.effective_chat.id, context)

        # RAG, если есть выбранные документы
        kb_ctx = None
        if self.kb_service and st.kb_selected_docs:
            try:
                chunks = await asyncio.to_thread(
                    self.kb_service.retrieve,
                    user_text,
                    st.kb_selected_docs,
                    int(getattr(self.settings, "rag_top_k", 8)),
                )
                kb_ctx = self.kb_service.build_context(chunks)
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
        if self.kb_service and st.kb_selected_docs:
            try:
                chunks = await asyncio.to_thread(
                    self.kb_service.retrieve,
                    transcript,
                    st.kb_selected_docs,
                    int(getattr(self.settings, "rag_top_k", 8)),
                )
                kb_ctx = self.kb_service.build_context(chunks)
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
        При получении файла/фото — только анализируем и описываем (НЕ добавляем в БЗ автоматически).
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

        # ==== KB callbacks ====
        if data.startswith("kb:page:"):
            if not self.kb_service:
                return
            page = int(data.split(":", 2)[2])
            docs, total_pages = await asyncio.to_thread(self.kb_service.list_docs, page, 10, None)
            await query.edit_message_reply_markup(
                reply_markup=self._kb_render_page(docs, page, total_pages, st)
            )
            return

        if data == "kb:clear":
            st.kb_selected_docs.clear()
            # обновим текущую страницу (берём 1)
            docs, total_pages = await asyncio.to_thread(self.kb_service.list_docs, 1, 10, None)
            await query.edit_message_reply_markup(
                reply_markup=self._kb_render_page(docs, 1, total_pages, st)
            )
            return

        if data == "kb:selected":
            if not st.kb_selected_docs:
                await query.edit_message_text("В контексте сейчас нет документов.")
                return
            lst = "\n".join(f"• {d}" for d in st.kb_selected_docs)
            await query.edit_message_text(f"В контексте:\n{lst}")
            return

        if data.startswith("kb:toggle:"):
            if not self.kb_service:
                return
            doc_id = data.split(":", 2)[2]
            # проверяем зашифрованность
            try:
                enc = await asyncio.to_thread(self.kb_service.is_encrypted, doc_id)
            except Exception:
                enc = False
            if doc_id in st.kb_selected_docs:
                st.kb_selected_docs.remove(doc_id)
            else:
                if enc:
                    self._kb_pwd_wait[user_id] = {"doc_id": doc_id, "tries": 0}
                    await query.edit_message_text("🔐 Документ защищен паролем. Введите пароль (3 попытки):")
                    return
                st.kb_selected_docs.append(doc_id)

            # перерисуем текущую страницу (пытаемся вычитать её из текста, иначе берем 1)
            page = 1
            try:
                # Хитрый, но безопасный способ: просто снова отрисуем 1 страницу
                docs, total_pages = await asyncio.to_thread(self.kb_service.list_docs, page, 10, None)
                await query.edit_message_reply_markup(
                    reply_markup=self._kb_render_page(docs, page, total_pages, st)
                )
            except Exception as e:
                await query.edit_message_text(f"Ошибка обновления списка: {e}")
            return
