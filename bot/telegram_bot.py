# bot/telegram_bot.py
from __future__ import annotations

import io
import asyncio
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
except ImportError as e:
    KB_AVAILABLE = False
    logger.warning("KB unavailable: %s", e)


# --- Простейшая in-memory модель диалогов ---
@dataclass
class DialogState:
    dialog_id: int
    title: str = "Диалог"
    created_at_ts: float = 0.0
    updated_at_ts: float = 0.0
    model: Optional[str] = None
    style: str = "Pro"  # Pro | Expert | User | CEO
    kb_selected_docs: List[str] = field(default_factory=list)  # список id документов


class ChatGPTTelegramBot:
    def __init__(self, openai, settings):
        self.openai = openai
        self.settings = settings

        # Разрешения
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

        # Диалоги (на пользователя)
        self._dialogs_by_user: Dict[int, Dict[int, DialogState]] = {}
        self._current_dialog_by_user: Dict[int, Optional[int]] = {}
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
                logger.exception("KB init failed: %s", e)

    # ------------- Команды/меню -------------
    def _build_commands(self) -> List[BotCommand]:
        return [
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

    async def _apply_bot_commands(self, bot, lang: Optional[str] = None) -> None:
        commands = self._build_commands()

        try:
            # Показать стандартную кнопку «Меню» с командами
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception:
            pass

        scopes = [
            BotCommandScopeDefault(),
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllChatAdministrators(),
        ]
        for scope in scopes:
            try:
                await bot.delete_my_commands(scope=scope)
            except Exception:
                pass
            await bot.set_my_commands(commands=commands, scope=scope, language_code=lang)

    async def setup_commands(self, app: Application) -> None:
        """
        Этот метод передаём в Application.builder().post_init(...)
        Он вызовется один раз после initialize().
        """
        lang = getattr(self.settings, "bot_language", None)
        # сначала без языка
        await self._apply_bot_commands(app.bot, lang=None)
        # затем локализованный набор, если нужен
        if lang:
            await self._apply_bot_commands(app.bot, lang=lang)
        logger.info("✅ Команды установлены (global scopes)")

    # ------------- Регистрация обработчиков -------------
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

        # Сообщения
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file_or_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        # Inline
        app.add_handler(CallbackQueryHandler(self.on_callback))

    # ------------- Вспомогательные -------------
    def _ensure_dialog(self, user_id: int) -> DialogState:
        user_dialogs = self._dialogs_by_user.setdefault(user_id, {})
        curr = self._current_dialog_by_user.get(user_id)
        if curr is None or curr not in user_dialogs:
            dlg_id = self._next_dialog_id
            self._next_dialog_id += 1
            st = DialogState(dialog_id=dlg_id)
            user_dialogs[dlg_id] = st
            self._current_dialog_by_user[user_id] = dlg_id
        return user_dialogs[self._current_dialog_by_user[user_id]]  # type: ignore[arg-type]

    def _list_dialogs(self, user_id: int) -> List[DialogState]:
        return list(self._dialogs_by_user.get(user_id, {}).values())

    async def _send_typing(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass

    async def _kb_titles_by_ids(self, ids: List[str]) -> List[str]:
        if not ids:
            return []
        if not (KB_AVAILABLE and self.kb_indexer):
            return ids
        list_fn = getattr(self.kb_indexer, "list_documents", None)
        if not callable(list_fn):
            return ids
        try:
            docs = await asyncio.to_thread(list_fn)
            by_id = {str(getattr(d, "id")): getattr(d, "title", str(getattr(d, "id"))) for d in docs}
            return [by_id.get(i, i) for i in ids]
        except Exception:
            return ids

    # ------------- Команды -------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_dialog(update.effective_user.id)
        await update.effective_message.reply_text(
            "Привет! Я готов к работе.\nКоманды: /help, /reset, /stats, /kb, /model, /mode, /dialogs, /img, /web"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "/reset — сброс контекста (новый диалог)\n"
            "/stats — статистика\n"
            "/kb — база знаний (выбор документов)\n"
            "/model — выбор модели OpenAI\n"
            "/mode — стиль ответов\n"
            "/dialogs — список диалогов (открыть/удалить/новый)\n"
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
        titles = await self._kb_titles_by_ids(st.kb_selected_docs)
        kb_list = ", ".join(titles) if titles else "—"
        model = st.model or getattr(self.settings, "openai_model", "gpt-4o")
        text = (
            "📊 Статистика:\n"
            f"- Диалог: {st.title}\n"
            f"- Модель: {model}\n"
            f"- Стиль: {st.style}\n"
            f"- Документов выбрано: {len(st.kb_selected_docs)}\n"
            f"- В контексте: {kb_list}"
        )
        await update.effective_message.reply_text(text)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        try:
            models = self.openai.list_models_for_menu()  # ожидается list[str]
        except Exception as e:
            logger.exception("list_models failed: %s", e)
            await update.effective_message.reply_text("Не удалось получить список моделей.")
            return

        current = st.model or getattr(self.settings, "openai_model", None)
        rows: List[List[InlineKeyboardButton]] = []
        for name in models:
            mark = "✅ " if name == current else ""
            rows.append([InlineKeyboardButton(f"{mark}{name}", callback_data=f"model:{name}")])

        await update.effective_message.reply_text(
            "Выберите модель:",
            reply_markup=InlineKeyboardMarkup(rows) if rows else None
        )

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        modes = ["Pro", "Expert", "User", "CEO"]
        rows: List[List[InlineKeyboardButton]] = []
        for m in modes:
            mark = "✅ " if st.style == m else ""
            rows.append([InlineKeyboardButton(f"{mark}{m}", callback_data=f"mode:{m}")])
        await update.effective_message.reply_text(
            "Выберите стиль ответа:",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    async def cmd_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        dialogs = self._list_dialogs(user_id)
        current_id = self._current_dialog_by_user.get(user_id)
        if not dialogs:
            await update.effective_message.reply_text("Диалогов пока нет. Нажмите /reset для создания нового.")
            return

        rows: List[List[InlineKeyboardButton]] = []
        for d in dialogs:
            title = d.title or f"Диалог #{d.dialog_id}"
            prefix = "⭐ " if d.dialog_id == current_id else ""
            # Кнопки: открыть | удалить
            rows.append([
                InlineKeyboardButton(f"{prefix}{title}", callback_data=f"open:{d.dialog_id}"),
                InlineKeyboardButton("🗑️", callback_data=f"del:{d.dialog_id}"),
            ])
        rows.append([InlineKeyboardButton("➕ Новый диалог", callback_data="newdlg")])

        await update.effective_message.reply_text(
            "Выберите диалог:",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    async def cmd_img(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.effective_message.reply_text("Использование: /img <описание изображения>")
            return
        prompt = " ".join(context.args)
        await self._send_typing(update.effective_chat.id, context)
        try:
            # generate_image возвращает (bytes, used_prompt)
            img_bytes, used_prompt = await asyncio.to_thread(
                self.openai.generate_image, prompt, None
            )
            bio = io.BytesIO(img_bytes)
            bio.name = "image.png"
            await update.effective_message.reply_photo(
                photo=InputFile(bio),
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
        """KB: админ при вызове /kb сначала делает sync(), затем всем показываем пикер документов."""
        st = self._ensure_dialog(update.effective_user.id)
        if not KB_AVAILABLE or not (self.kb_indexer and self.kb_retriever and self.kb_ctx):
            await update.effective_message.reply_text("Модуль базы знаний недоступен в этой сборке.")
            return

        user_id = update.effective_user.id
        if user_id in self.admin_ids:
            try:
                added, updated, deleted, unchanged = await asyncio.to_thread(self.kb_indexer.sync)
                await update.effective_message.reply_text(
                    "Синхронизация завершена:\n"
                    f"• Добавлено: {added}\n"
                    f"• Обновлено: {updated}\n"
                    f"• Удалено: {deleted}\n"
                    f"• Без изменений: {unchanged}"
                )
            except Exception as e:
                logger.warning("KB sync failed: %s", e)

        await self._kb_render_picker(update, context, st)

    async def _kb_render_picker(self, update: Update, context: ContextTypes.DEFAULT_TYPE, st: DialogState):
        list_fn = getattr(self.kb_indexer, "list_documents", None) if self.kb_indexer else None
        if not callable(list_fn):
            await update.effective_message.reply_text(
                "Не удалось получить список документов: Indexer не предоставляет list_documents()."
            )
            return

        try:
            docs = await asyncio.to_thread(list_fn)
        except Exception as e:
            logger.exception("list_documents failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка получения списка документов: {e}")
            return

        if not docs:
            await update.effective_message.reply_text(
                "Документов пока нет. Загрузите их на Я.Диск и запустите синхронизацию (админом)."
            )
            return

        selected = set(st.kb_selected_docs or [])
        rows: List[List[InlineKeyboardButton]] = []
        for d in docs:
            doc_id = str(getattr(d, "id"))
            title = getattr(d, "title", doc_id) or doc_id
            mark = "✅ " if doc_id in selected else "⬜ "
            shown = title if len(title) <= 48 else title[:45] + "…"
            rows.append([InlineKeyboardButton(f"{mark}{shown}", callback_data=f"kb:toggle_doc:{doc_id}")])

        rows.append([
            InlineKeyboardButton("Очистить выбор", callback_data="kb:clear"),
            InlineKeyboardButton("Готово", callback_data="kb:done"),
        ])

        await update.effective_message.reply_text(
            "Выберите документы (повторное нажатие снимает выбор):",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    # ------------- Сообщения -------------
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        user_text = update.effective_message.text

        await self._send_typing(update.effective_chat.id, context)

        kb_ctx = None
        if st.kb_selected_docs and KB_AVAILABLE and self.kb_retriever and self.kb_ctx:
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
        if st.kb_selected_docs and KB_AVAILABLE and self.kb_retriever and self.kb_ctx:
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
        По файлам/фото: только анализ (описание). В БЗ добавление — через /kb (админ-синк + выбор).
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

    # ------------- Inline callbacks -------------
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return
        await query.answer()

        data = query.data or ""
        user_id = update.effective_user.id
        st = self._ensure_dialog(user_id)

        # --- model:<name>
        if data.startswith("model:"):
            name = data.split(":", 1)[1]
            st.model = name
            await query.edit_message_text(f"Модель установлена: {name}")
            return

        # --- mode:<name>
        if data.startswith("mode:"):
            name = data.split(":", 1)[1]
            st.style = name
            await query.edit_message_text(f"Стиль установлен: {name}")
            return

        # --- dialogs
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
                await query.edit_message_text(f"Диалог #{dlg_id} удалён.")
            else:
                await query.edit_message_text("Диалог не найден.")
            return

        # --- KB: новый интерфейс выбора документов
        if data.startswith("kb:toggle_doc:"):
            doc_id = data.rsplit(":", 1)[1]
            cur = set(st.kb_selected_docs or [])
            if doc_id in cur:
                cur.remove(doc_id)
            else:
                cur.add(doc_id)
            st.kb_selected_docs = list(cur)
            # Перерисуем пикер
            await query.edit_message_reply_markup(reply_markup=None)
            await self._kb_render_picker(update, context, st)
            return

        if data == "kb:clear":
            st.kb_selected_docs = []
            await query.edit_message_reply_markup(reply_markup=None)
            await self._kb_render_picker(update, context, st)
            return

        if data == "kb:done":
            titles = await self._kb_titles_by_ids(st.kb_selected_docs)
            picked = ", ".join(titles) if titles else "ничего не выбрано"
            await query.edit_message_text(f"Готово. Выбрано: {picked}")
            return

        # --- KB: обратная совместимость (если где-то остался вызов)
        if data == "kb:pick":
            await query.edit_message_reply_markup(reply_markup=None)
            await self._kb_render_picker(update, context, st)
            return

        if data == "kb:sync":
            if not KB_AVAILABLE or not self.kb_indexer:
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
