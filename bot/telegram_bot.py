from __future__ import annotations

import asyncio
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
logging.basicConfig(level=logging.INFO)
logging.getLogger("bot.knowledge_base.retriever").setLevel(logging.DEBUG)
logging.getLogger("bot.knowledge_base.indexer").setLevel(logging.DEBUG)
# --- Опциональная База Знаний (KB) ---
KB_AVAILABLE = True
try:
    from bot.knowledge_base.indexer import KnowledgeBaseIndexer
    from bot.knowledge_base.retriever import KnowledgeBaseRetriever
except ImportError as e:
    KB_AVAILABLE = False
    logger.warning("KB unavailable (import): %s", e)


# --- Простая модель диалогов в памяти ---
@dataclass
class DialogState:
    dialog_id: int
    title: str = "Диалог"
    created_at_ts: float = 0.0
    updated_at_ts: float = 0.0
    model: Optional[str] = None
    style: str = "Pro"

    # KB
    kb_selected_docs: List[str] = field(default_factory=list)
    # Пароли к документам в рамках СЕССИИ: disk:/... -> password
    kb_passwords: Dict[str, str] = field(default_factory=dict)
    # временный флаг «ждём ввод пароля» (индекс документа из последней клавы)
    kb_await_pwd_for_path: Optional[str] = None


class ChatGPTTelegramBot:
    def __init__(self, openai, settings):
        self.openai = openai
        self.settings = settings

        # Разрешения (оставляем как было)
        self.admin_ids = set(getattr(settings, "admin_user_ids", []) or getattr(settings, "admin_set", []) or [])
        self.allowed_ids = set(getattr(settings, "allowed_user_ids", []) or getattr(settings, "allowed_set", []) or [])

        # Состояние пользователей
        self._dialogs_by_user: Dict[int, Dict[int, DialogState]] = {}
        self._current_dialog_by_user: Dict[int, int] = {}
        self._next_dialog_id: int = 1

        # KB
        self.kb_indexer: Optional[KnowledgeBaseIndexer] = None
        self.kb_retriever: Optional[KnowledgeBaseRetriever] = None
        if KB_AVAILABLE:
            try:
                self.kb_indexer = KnowledgeBaseIndexer(settings)
                self.kb_retriever = KnowledgeBaseRetriever(settings)
            except Exception as e:
                logger.exception("KB init failed: %s", e)

    # ========= Команды/меню =========
    def _build_commands(self) -> List[BotCommand]:
        return [
            BotCommand("start", "Запуск и меню"),
            BotCommand("help", "Помощь"),
            BotCommand("reset", "Сброс контекста"),
            BotCommand("stats", "Статистика"),
            BotCommand("kb", "База знаний"),
            BotCommand("model", "Выбор модели"),
            BotCommand("mode", "Стиль ответов"),
            BotCommand("dialogs", "Список диалогов"),
            BotCommand("img", "Сгенерировать изображение"),
            BotCommand("web", "Поиск в интернете"),
            BotCommand("kb_diag", "Диагностика БЗ"),
        ]

    async def setup_commands(self, app: Application) -> None:
        # Применение команд во всех scope
        commands = self._build_commands()
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception:
            pass
        scopes = [BotCommandScopeDefault(), BotCommandScopeAllPrivateChats(), BotCommandScopeAllChatAdministrators()]
        for scope in scopes:
            try:
                await app.bot.delete_my_commands(scope=scope)
            except Exception:
                pass
            await app.bot.set_my_commands(commands=commands, scope=scope)

        logger.info("✅ Команды установлены (global scopes)")

    # ========= Регистрация обработчиков =========
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
        app.add_handler(CommandHandler("kb_diag", self.cmd_kb_diag))

        # Сообщения
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file_or_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        # Inline callbacks
        app.add_handler(CallbackQueryHandler(self.on_callback))

    # ========= Вспомогательные =========
    def _ensure_dialog(self, user_id: int) -> DialogState:
        user_dialogs = self._dialogs_by_user.setdefault(user_id, {})
        if user_id not in self._current_dialog_by_user:
            dlg_id = self._next_dialog_id
            self._next_dialog_id += 1
            st = DialogState(dialog_id=dlg_id, created_at_ts=time.time())
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

    # ========= Команды =========
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_dialog(update.effective_user.id)
        await update.effective_message.reply_text(
            "Привет! Я готов к работе.\nКоманды: /help, /reset, /stats, /kb, /model, /mode, /dialogs, /img, /web"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "/reset — сброс контекста (новый диалог)\n"
            "/stats — статистика\n"
            "/kb — база знаний (выбор документов, пароли к PDF)\n"
            "/model — выбор модели\n"
            "/mode — стиль ответов\n"
            "/dialogs — список диалогов\n"
            "/img — сгенерировать изображение\n"
            "/web — веб-поиск\n"
            "/kb_diag — диагностика БЗ\n"
        )
        await update.effective_message.reply_text(text)

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        dlg_id = self._next_dialog_id
        self._next_dialog_id += 1
        self._dialogs_by_user.setdefault(user_id, {})[dlg_id] = DialogState(dialog_id=dlg_id, created_at_ts=time.time())
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
            img_bytes, used_prompt = await asyncio.to_thread(
                self.openai.generate_image, prompt, None
            )
            await update.effective_message.reply_photo(
                photo=InputFile.from_bytes(img_bytes, filename="image.png"),
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
            await update.effective_message.reply_text(f"Ошибка веб-поиска: {e}")

    async def cmd_kb_diag(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not (KB_AVAILABLE and self.kb_indexer):
            await update.effective_message.reply_text("Диагностика: модуль БЗ недоступен.")
            return
        try:
            text = await asyncio.to_thread(self.kb_indexer.diagnose, 200)
        except Exception as e:
            text = f"Ошибка диагностики: {e}"
        await update.effective_message.reply_text(text)

    async def cmd_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        if not (KB_AVAILABLE and self.kb_indexer and self.kb_retriever):
            await update.effective_message.reply_text("Модуль базы знаний недоступен в этой сборке.")
            return

        # 1) Синхронизация
        try:
            added, updated, deleted, unchanged = await asyncio.to_thread(self.kb_indexer.sync)
            sync_msg = (
                f"Синхронизация завершена: +{added}, обновлено {updated}, "
                f"удалено {deleted}, без изменений {unchanged}."
            )
        except Exception as e:
            logger.exception("KB sync failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка синхронизации: {e}")
            return

        # 2) Список документов
        try:
            docs = await asyncio.to_thread(self.kb_indexer.list_documents)
        except Exception as e:
            logger.exception("list_documents failed: %s", e)
            await update.effective_message.reply_text("Не удалось получить список документов.")
            return

        # 3) Кнопки выбора (и кнопка «🔑 Пароль»)
        rows: List[List[InlineKeyboardButton]] = []
        # индексируем, чтобы не передавать длинные пути в callback_data
        path_by_idx: Dict[int, str] = {}
        for i, d in enumerate(docs):
            path_by_idx[i] = d.path
            mark = "✅ " if d.path in st.kb_selected_docs else "☐ "
            rows.append([
                InlineKeyboardButton(f"{mark}{d.path.split('/')[-1]}", callback_data=f"kb:toggle:{i}"),
            ])
        # Сервисные
        rows.append([
            InlineKeyboardButton("💾 Сохранить выбор", callback_data="kb:save"),
            InlineKeyboardButton("🔁 Повторить синхронизацию", callback_data="kb:resync"),
        ])
        # Кнопки «🔑 Пароль к файлу …» постранично делать необязательно — запрашиваем пароль после тапа по «ключу» под активной записью.
        # Сформируем отдельный ряд с кнопками «Пароль» для уже отмеченных файлов:
        for i, d in enumerate(docs):
            if d.path in st.kb_selected_docs:
                rows.append([
                    InlineKeyboardButton(f"🔑 Пароль к: {d.path.split('/')[-1]}", callback_data=f"kb:pwd:{i}")
                ])

        # Сохраняем таблицу индексов в контекст приложения (на 2 минуты)
        context.chat_data["kb_idx_map"] = {"ts": time.time(), "paths": path_by_idx}

        await update.effective_message.reply_text(
            sync_msg + "\n\nВыберите документы:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    # ========= Сообщения =========
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        user_text = update.effective_message.text or ""

        # Блок ввода пароля к PDF (только один «ожидаемый» одновременно)
        if st.kb_await_pwd_for_path:
            pwd = user_text.strip()
            target = st.kb_await_pwd_for_path
            st.kb_passwords[target] = pwd
            st.kb_await_pwd_for_path = None
            await update.effective_message.reply_text(f"🔑 Пароль сохранён для: {target.split('/')[-1]}")
            return

        await self._send_typing(update.effective_chat.id, context)

        # Построение KB-контекста (если выбранные документы есть)
        # ...
        kb_ctx = None
        if KB_AVAILABLE and self.kb_retriever and st.kb_selected_docs:
            try:
                logger.debug(
                    "KB call: docs=%s, passwords=%s",
                    st.kb_selected_docs,
                    {k: "***" for k in st.kb_passwords.keys()}  # не логируем сами пароли
                )
                chunks = await asyncio.to_thread(
                    self.kb_retriever.retrieve,
                    user_text,
                    list(st.kb_selected_docs),
                    st.kb_passwords
                )
                if chunks:
                    kb_ctx = "\n\n".join(chunks)
                logger.debug("KB result: chunks=%d, ctx_len=%d", len(chunks or []), len(kb_ctx or ""))
            except Exception as e:
                logger.warning("KB retrieve failed: %s", e)

        # Вызов OpenAI
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
        if KB_AVAILABLE and self.kb_retriever and st.kb_selected_docs:
            try:
                logger.debug(
                    "KB call: docs=%s, passwords=%s",
                    st.kb_selected_docs,
                    {k: "***" for k in st.kb_passwords.keys()}  # не логируем сами пароли
                )
                chunks = await asyncio.to_thread(
                    self.kb_retriever.retrieve,
                    user_text,
                    list(st.kb_selected_docs),
                    st.kb_passwords
                )
                if chunks:
                    kb_ctx = "\n\n".join(chunks)
                logger.debug("KB result: chunks=%d, ctx_len=%d", len(chunks or []), len(kb_ctx or ""))
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

        data = (query.data or "").strip()
        user_id = update.effective_user.id
        st = self._ensure_dialog(user_id)

        # Работа с моделью/режимом/диалогами (как было)
        if data.startswith("model:"):
            name = data.split(":", 1)[1]
            st.model = name
            await query.edit_message_text(f"Модель установлена: {name}")
            return

        if data.startswith("mode:"):
            name = data.split(":", 1)[1]
            st.style = name
            await query.edit_message_text(f"Стиль установлен: {name}")
            return

        if data == "newdlg":
            dlg_id = self._next_dialog_id
            self._next_dialog_id += 1
            self._dialogs_by_user.setdefault(user_id, {})[dlg_id] = DialogState(dialog_id=dlg_id, created_at_ts=time.time())
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

        # ===== KB callbacks =====
        if data == "kb:resync":
            # просто перезапускаем /kb
            await self.cmd_kb(update, context)
            return

        if data == "kb:save":
            await query.edit_message_text(f"Выбрано документов: {len(st.kb_selected_docs)}. Буду использовать БЗ.")
            return

        # map индексов
        kb_idx_map = context.chat_data.get("kb_idx_map") or {}
        paths: Dict[int, str] = (kb_idx_map.get("paths") or {}) if isinstance(kb_idx_map, dict) else {}

        if data.startswith("kb:toggle:"):
            try:
                idx = int(data.split(":", 2)[2])
                path = paths.get(idx)
                if not path:
                    await query.answer("Элемент недоступен, обновите список (/kb).", show_alert=True)
                    return
                if path in st.kb_selected_docs:
                    st.kb_selected_docs.remove(path)
                    st.kb_passwords.pop(path, None)  # пароль стираем, если сняли выбор
                else:
                    st.kb_selected_docs.append(path)
                await query.answer("Ок")
            except Exception as e:
                await query.answer(f"Ошибка: {e}", show_alert=True)
            return

        if data.startswith("kb:pwd:"):
            try:
                idx = int(data.split(":", 2)[2])
                path = paths.get(idx)
                if not path:
                    await query.answer("Элемент недоступен, обновите список (/kb).", show_alert=True)
                    return
                if path not in st.kb_selected_docs:
                    await query.answer("Сначала отметьте документ галочкой.", show_alert=True)
                    return
                st.kb_await_pwd_for_path = path
                await query.edit_message_text(
                    f"🔑 Введите пароль для файла: {path.split('/')[-1]}\n"
                    f"Ответьте сообщением с паролем. Пароль сохранится только в рамках текущего диалога."
                )
            except Exception as e:
                await query.answer(f"Ошибка: {e}", show_alert=True)
            return
