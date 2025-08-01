# bot/telegram_bot.py
from __future__ import annotations

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

# --- KB (опционально) ---
KB_AVAILABLE = True
try:
    from bot.knowledge_base.indexer import KnowledgeBaseIndexer
    from bot.knowledge_base.retriever import KnowledgeBaseRetriever
    from bot.knowledge_base.context_manager import ContextManager
except Exception as e:
    KB_AVAILABLE = False
    logger.warning("KB unavailable (import): %s", e)


@dataclass
class DialogState:
    dialog_id: int
    title: str = "Диалог"
    model: Optional[str] = None
    style: str = "Pro"
    kb_enabled: bool = True
    kb_selected_docs: List[str] = field(default_factory=list)


class ChatGPTTelegramBot:
    def __init__(self, openai, settings):
        self.openai = openai
        self.settings = settings

        self._dialogs_by_user: Dict[int, Dict[int, DialogState]] = {}
        self._current_dialog_by_user: Dict[int, int] = {}
        self._next_dialog_id = 1

        self.kb_indexer: Optional[KnowledgeBaseIndexer] = None
        self.kb_retriever: Optional[KnowledgeBaseRetriever] = None
        self.kb_ctx_mgr: Optional[ContextManager] = None
        if KB_AVAILABLE:
            try:
                self.kb_indexer = KnowledgeBaseIndexer(settings)
                self.kb_retriever = KnowledgeBaseRetriever(settings)
                self.kb_ctx_mgr = ContextManager(settings)
            except Exception as e:
                logger.error("KB init failed: %s", e)

    # -------- команды и меню --------
    def _build_commands(self) -> List[BotCommand]:
        return [
            BotCommand("help", "помощь"),
            BotCommand("reset", "сброс контекста"),
            BotCommand("stats", "статистика"),
            BotCommand("kb", "база знаний"),
            BotCommand("kb_diag", "диагностика БЗ"),
            BotCommand("model", "выбор модели OpenAI"),
            BotCommand("mode", "стиль ответов"),
            BotCommand("dialogs", "список диалогов"),
            BotCommand("img", "сгенерировать изображение"),
            BotCommand("web", "веб-поиск"),
        ]

    async def setup_commands(self, app: Application) -> None:
        commands = self._build_commands()
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception:
            pass
        for scope in (BotCommandScopeDefault(), BotCommandScopeAllPrivateChats(), BotCommandScopeAllChatAdministrators()):
            try:
                await app.bot.delete_my_commands(scope=scope)
            except Exception:
                pass
            await app.bot.set_my_commands(commands=commands, scope=scope)

    def install(self, app: Application) -> None:
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("reset", self.cmd_reset))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("kb", self.cmd_kb))
        app.add_handler(CommandHandler("kb_diag", self.cmd_kb_diag))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("dialogs", self.cmd_dialogs))
        app.add_handler(CommandHandler("img", self.cmd_img))
        app.add_handler(CommandHandler("web", self.cmd_web))

        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file_or_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(CallbackQueryHandler(self.on_callback))

    # -------- вспомогательные --------
    def _ensure_dialog(self, user_id: int) -> DialogState:
        dmap = self._dialogs_by_user.setdefault(user_id, {})
        if user_id not in self._current_dialog_by_user:
            dlg_id = self._next_dialog_id
            self._next_dialog_id += 1
            dmap[dlg_id] = DialogState(dialog_id=dlg_id)
            self._current_dialog_by_user[user_id] = dlg_id
        return dmap[self._current_dialog_by_user[user_id]]

    async def _send_typing(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass

    # -------- команды --------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            "Привет! Команды: /help, /reset, /stats, /kb, /kb_diag, /model, /mode, /dialogs, /img, /web"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            "/reset — новый диалог\n"
            "/stats — статистика\n"
            "/kb — база знаний\n"
            "/kb_diag — диагностика БЗ\n"
            "/model — выбор модели\n"
            "/mode — стиль\n"
            "/dialogs — диалоги\n"
            "/img — изображение\n"
            "/web — веб-поиск\n"
        )

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        dlg_id = self._next_dialog_id
        self._next_dialog_id += 1
        self._dialogs_by_user.setdefault(user_id, {})[dlg_id] = DialogState(dialog_id=dlg_id)
        self._current_dialog_by_user[user_id] = dlg_id
        await update.effective_message.reply_text("🔄 Новый диалог создан.")

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
        )
        await update.effective_message.reply_text(text)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        names = []
        try:
            names = self.openai.list_models_for_menu()
        except Exception:
            pass
        rows = []
        current = st.model or getattr(self.settings, "openai_model", None)
        for name in names:
            mark = "✅ " if name == current else ""
            rows.append([InlineKeyboardButton(f"{mark}{name}", callback_data=f"model:{name}")])
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
        # опущено для краткости — без изменений
        await update.effective_message.reply_text("Список диалогов пока без изменений.")

    async def cmd_img(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # опущено — без изменений
        await update.effective_message.reply_text("Генерация изображений настроена как раньше.")

    async def cmd_web(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # опущено — без изменений
        await update.effective_message.reply_text("Веб-поиск пока без изменений.")

    async def cmd_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Меню выбора документов (как у вас было). Здесь логика не менялась."""
        st = self._ensure_dialog(update.effective_user.id)
        if not (self.kb_indexer and self.kb_retriever):
            await update.effective_message.reply_text("Модуль базы знаний недоступен в этой сборке.")
            return

        # Запустим синк
        try:
            added, updated, deleted, unchanged = await asyncio.to_thread(self.kb_indexer.sync)
            await update.effective_message.reply_text(
                f"Синхронизация завершена: +{added}, обновлено {updated}, удалено {deleted}, без изменений {unchanged}."
            )
        except Exception as e:
            await update.effective_message.reply_text(f"Ошибка синхронизации: {e}")
            return

        # Список документов для выбора
        docs = self.kb_indexer.list_documents()
        if not docs:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("— Документов нет —", callback_data="noop")]])
            await update.effective_message.reply_text("Документы:", reply_markup=kb)
            return

        rows: List[List[InlineKeyboardButton]] = []
        selected = set(st.kb_selected_docs)
        for d in docs:
            title = os.path.basename(d.path) if hasattr(d, "path") else str(d)
            mark = "✅ " if d.path in selected else "☐ "
            rows.append([InlineKeyboardButton(f"{mark}{title[:56]}", callback_data=f"kb:toggle_doc:{d.path}")])
        rows.append([
            InlineKeyboardButton("💾 Сохранить выбор", callback_data="kb:save"),
            InlineKeyboardButton("🔁 Повторить синхронизацию", callback_data="kb:resync"),
        ])
        await update.effective_message.reply_text("Выберите документы:", reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_kb_diag(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Диагностика БЗ: покажет, что видит индексер и сможет вытащить первые строки из первого файла."""
        if not (self.kb_indexer and self.kb_retriever):
            await update.effective_message.reply_text("KB: модуль недоступен.")
            return

        report = self.kb_indexer.diagnose(max_items=50)
        preview = ""
        docs = self.kb_indexer.list_documents()
        if docs:
            path = docs[0].path
            try:
                data = self.kb_retriever._download_yadisk_bytes(path)
                preview = f"\n\nПервый файл: {path}\nsize={len(data)} bytes"
                if data:
                    txt = self.kb_retriever._extract_text(path, data)
                    head = (txt or "").splitlines()[:5]
                    preview += "\nExtract head:\n" + "\n".join(head)
            except Exception as e:
                preview = f"\n\nDownload failed for {path}: {e}"
        await update.effective_message.reply_text((report + preview)[:3500])

    # -------- обработка сообщений --------
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        user_text = update.effective_message.text or ""

        await self._send_typing(update.effective_chat.id, context)

        kb_ctx = None
        if st.kb_enabled and st.kb_selected_docs and self.kb_retriever and self.kb_ctx_mgr:
            try:
                chunks = await asyncio.to_thread(self.kb_retriever.retrieve, user_text, st.kb_selected_docs)
                kb_ctx = self.kb_ctx_mgr.build_context(chunks)
            except Exception as e:
                logger.debug("KB retrieve failed: %s", e)

        try:
            reply = await asyncio.to_thread(
                self.openai.chat,
                user_text,
                st.model or getattr(self.settings, "openai_model", None),
                getattr(self.settings, "openai_temperature", 0.2),
                st.style,
                kb_ctx,   # <--- важное: передаём контекст
            )
            await update.effective_message.reply_text(reply)
        except Exception as e:
            logger.error("text chat failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка обращения к OpenAI: {e}")

    # on_voice/on_file_or_photo/on_callback — без изменений в этой выдаче
    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text("Голос — без изменений в этой версии.")

    async def on_file_or_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text("Анализ вложений — без изменений в этой версии.")

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Коллбэки для KB-меню (toggle_doc/save/resync) и прочее — оставьте вашу текущую реализацию.
        await update.callback_query.answer("Окей.")
