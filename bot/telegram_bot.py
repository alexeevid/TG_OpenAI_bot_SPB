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
except Exception as e:
    KB_AVAILABLE = False
    logger.warning("KB unavailable: %s", e)


# --- Диалог ---
@dataclass
class DialogState:
    dialog_id: int
    title: str = "Диалог"
    model: Optional[str] = None
    style: str = "Pro"
    kb_selected_ids: List[str] = field(default_factory=list)  # ids документов
    rag_mode: str = "off"  # "db", "on-disk", "off"


class ChatGPTTelegramBot:
    def __init__(self, openai, settings):
        self.openai = openai
        self.settings = settings

        self.admin_ids = set(getattr(settings, "admin_user_ids", []) or getattr(settings, "admin_set", []) or [])
        self.allowed_ids = set(getattr(settings, "allowed_user_ids", []) or getattr(settings, "allowed_set", []) or [])

        self._dialogs_by_user: Dict[int, Dict[int, DialogState]] = {}
        self._current_dialog_by_user: Dict[int, int] = {}
        self._next_dialog_id = 1

        self.kb_indexer: Optional[KnowledgeBaseIndexer] = None
        self.kb_retriever: Optional[KnowledgeBaseRetriever] = None
        if KB_AVAILABLE:
            try:
                self.kb_indexer = KnowledgeBaseIndexer(settings)
                self.kb_retriever = KnowledgeBaseRetriever(settings)
            except Exception as e:
                logger.error("KB init failed: %s", e)

    # ===== Меню/команды ======================================================
    def _commands(self) -> List[BotCommand]:
        return [
            BotCommand("help", "помощь"),
            BotCommand("reset", "новый диалог"),
            BotCommand("stats", "статистика"),
            BotCommand("kb", "база знаний"),
            BotCommand("model", "выбор модели"),
            BotCommand("mode", "стиль ответа"),
            BotCommand("dialogs", "диалоги"),
            BotCommand("img", "сгенерировать изображение"),
            BotCommand("web", "веб-поиск"),
        ]

    async def setup_commands(self, app: Application) -> None:
        """Вызывается из Application.builder().post_init(self.setup_commands)"""
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception:
            pass
        try:
            await app.bot.delete_my_commands()
        except Exception:
            pass
        await app.bot.set_my_commands(self._commands())
        logger.info("✅ Команды установлены (global scopes)")

    # ===== Установка хэндлеров ================================================
    def install(self, app: Application) -> None:
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

        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file_or_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        app.add_handler(CallbackQueryHandler(self.on_callback))

    # ===== Вспомогательные ====================================================
    def _ensure_dialog(self, user_id: int) -> DialogState:
        user_dialogs = self._dialogs_by_user.setdefault(user_id, {})
        if user_id not in self._current_dialog_by_user or self._current_dialog_by_user[user_id] not in user_dialogs:
            dlg_id = self._next_dialog_id
            self._next_dialog_id += 1
            st = DialogState(dialog_id=dlg_id)
            user_dialogs[dlg_id] = st
            self._current_dialog_by_user[user_id] = dlg_id
        return user_dialogs[self._current_dialog_by_user[user_id]]

    async def _typing(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass

    # ===== Команды ============================================================
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._ensure_dialog(update.effective_user.id)
        await update.effective_message.reply_text(
            "Привет! Я готов к работе.\n"
            "Команды: /help, /reset, /stats, /kb, /model, /mode, /dialogs, /img, /web"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            "/reset — новый диалог\n"
            "/stats — статистика\n"
            "/kb — база знаний (синхронизация и выбор документов)\n"
            "/model — выбор модели\n"
            "/mode — стиль ответа (Pro/Expert/User/CEO)\n"
            "/dialogs — список диалогов (открыть/удалить)\n"
            "/img — сгенерировать изображение\n"
            "/web — веб-поиск\n"
        )

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        dlg_id = self._next_dialog_id
        self._next_dialog_id += 1
        self._dialogs_by_user.setdefault(user_id, {})[dlg_id] = DialogState(dialog_id=dlg_id)
        self._current_dialog_by_user[user_id] = dlg_id
        await update.effective_message.reply_text("🔄 Новый диалог создан. Контекст очищен.")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        kb_count = len(st.kb_selected_ids)
        rag = st.rag_mode
        await update.effective_message.reply_text(
            "📊 Статистика:\n"
            f"- Диалог: {st.title}\n"
            f"- Модель: {st.model or 'gpt-4o'}\n"
            f"- Стиль: {st.style}\n"
            f"- RAG: {rag}\n"
            f"- Документов в контексте: {kb_count}"
        )

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        current = st.model or "gpt-4o"
        models = ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini", "o3-mini", "o1-mini"]
        rows = []
        for m in models:
            mark = "✅ " if m == current else ""
            rows.append([InlineKeyboardButton(f"{mark}{m}", callback_data=f"model:{m}")])
        await update.effective_message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        modes = ["Pro", "Expert", "User", "CEO"]
        rows = []
        for m in modes:
            mark = "✅ " if st.style == m else ""
            rows.append([InlineKeyboardButton(f"{mark}{m}", callback_data=f"mode:{m}")])
        await update.effective_message.reply_text("Выберите стиль:", reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        dialogs = list(self._dialogs_by_user.get(user_id, {}).values())
        cur = self._current_dialog_by_user.get(user_id)
        if not dialogs:
            await update.effective_message.reply_text("Диалогов пока нет. Нажмите /reset для создания нового.")
            return
        rows = []
        for d in dialogs:
            prefix = "⭐ " if d.dialog_id == cur else ""
            rows.append([
                InlineKeyboardButton(f"{prefix}{d.title}", callback_data=f"open:{d.dialog_id}"),
                InlineKeyboardButton("🗑️", callback_data=f"del:{d.dialog_id}"),
            ])
        rows.append([InlineKeyboardButton("➕ Новый диалог", callback_data="newdlg")])
        await update.effective_message.reply_text("Выберите диалог:", reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_img(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.effective_message.reply_text("Использование: /img <описание>")
            return
        prompt = " ".join(context.args)
        await self._typing(update.effective_chat.id, context)
        try:
            img_bytes, used_prompt = await asyncio.to_thread(self.openai.generate_image, prompt, None)
            buf = io.BytesIO(img_bytes)
            buf.name = "image.png"
            await update.effective_message.reply_photo(photo=buf, caption=f"🖼️ Prompt:\n{used_prompt}")
        except Exception as e:
            logger.exception("Image generation failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка генерации изображения: {e}")

    async def cmd_web(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.effective_message.reply_text("Использование: /web <запрос>")
            return
        query = " ".join(context.args)
        await self._typing(update.effective_chat.id, context)
        try:
            answer, sources = await asyncio.to_thread(self.openai.web_answer, query)
            src = "\n\nИсточники:\n" + "\n".join(f"• {u}" for u in sources) if sources else "\n\n(источники не найдены)"
            await update.effective_message.reply_text(answer + src)
        except Exception as e:
            logger.exception("web failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка веб-поиска: {e}")

    async def cmd_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Автосинк + меню выбора документов (чекбоксы)."""
        st = self._ensure_dialog(update.effective_user.id)
        if not (KB_AVAILABLE and self.kb_indexer and self.kb_retriever):
            await update.effective_message.reply_text("Модуль базы знаний недоступен в этой сборке.")
            return

        # 1) Синхронизация
        try:
            added, updated, deleted, unchanged = await asyncio.to_thread(self.kb_indexer.sync)
            sync_note = ""
            if (added or updated or deleted):
                sync_note = f" (обновлено: +{added}/~{updated}/–{deleted})"
        except Exception as e:
            logger.exception("KB sync error: %s", e)
            await update.effective_message.reply_text(f"Ошибка синхронизации: {e}")
            return

        # 2) Каталог документов
        try:
            docs = await asyncio.to_thread(self.kb_indexer.list_documents)
        except Exception as e:
            logger.exception("KB list_documents error: %s", e)
            await update.effective_message.reply_text(f"Не удалось получить список документов: {e}")
            return

        if not docs:
            await update.effective_message.reply_text("В базе знаний пока нет документов." + (sync_note or ""))
            return

        # 3) Показать чекбоксы (до 10; при необходимости добавить пагинацию)
        rows: List[List[InlineKeyboardButton]] = []
        selected = set(st.kb_selected_ids)
        for d in docs[:10]:
            doc_id = d.get("id")
            mark = "✅" if doc_id in selected else "☐"
            title = d.get("title") or d.get("path") or doc_id
            rows.append([InlineKeyboardButton(f"{mark} {title}", callback_data=f"kb:toggle:{doc_id}")])
        rows.append([InlineKeyboardButton("Готово", callback_data="kb:done")])
        await update.effective_message.reply_text("База знаний" + (sync_note or "") + " — выберите документы:",
                                                  reply_markup=InlineKeyboardMarkup(rows))

        st.rag_mode = self.kb_retriever.mode if self.kb_retriever else "off"

    # ===== Сообщения ==========================================================
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        text = update.effective_message.text or ""
        await self._typing(update.effective_chat.id, context)

        kb_ctx = None
        if st.kb_selected_ids and KB_AVAILABLE and self.kb_retriever:
            try:
                chunks = await asyncio.to_thread(self.kb_retriever.retrieve, text, st.kb_selected_ids)
                if chunks:
                    parts = []
                    for ch in chunks[:6]:
                        parts.append(f"[{ch.get('title')}] {ch.get('chunk')}")
                    kb_ctx = "\n\n".join(parts)
            except Exception as e:
                logger.warning("KB retrieve failed: %s", e)

        try:
            reply = await asyncio.to_thread(
                self.openai.chat, text, st.model or "gpt-4o", 0.2, st.style, kb_ctx
            )
            await update.effective_message.reply_text(reply)
        except Exception as e:
            logger.exception("chat failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка обращения к OpenAI: {e}")

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._ensure_dialog(update.effective_user.id)
        file = await update.effective_message.voice.get_file()
        raw = await file.download_as_bytearray()
        await self._typing(update.effective_chat.id, context)

        try:
            transcript = await asyncio.to_thread(self.openai.transcribe_audio, bytes(raw))
        except Exception as e:
            logger.exception("transcribe failed: %s", e)
            await update.effective_message.reply_text(f"Не удалось распознать аудио: {e}")
            return

        kb_ctx = None
        if st.kb_selected_ids and KB_AVAILABLE and self.kb_retriever:
            try:
                chunks = await asyncio.to_thread(self.kb_retriever.retrieve, transcript, st.kb_selected_ids)
                if chunks:
                    parts = []
                    for ch in chunks[:6]:
                        parts.append(f"[{ch.get('title')}] {ch.get('chunk')}")
                    kb_ctx = "\n\n".join(parts)
            except Exception as e:
                logger.warning("KB retrieve failed: %s", e)

        try:
            answer = await asyncio.to_thread(self.openai.chat, transcript, st.model or "gpt-4o", 0.2, st.style, kb_ctx)
            await update.effective_message.reply_text(f"🎙️ Вы сказали: {transcript}\n\nОтвет:\n{answer}")
        except Exception as e:
            logger.exception("voice answer failed: %s", e)
            await update.effective_message.reply_text(f"Ошибка обращения к OpenAI: {e}")

    async def on_file_or_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Только анализ вложений; в БЗ добавляем через /kb."""
        msg = update.effective_message
        await self._typing(update.effective_chat.id, context)
        try:
            if msg.document:
                f = await msg.document.get_file()
                raw = await f.download_as_bytearray()
                desc = await asyncio.to_thread(self.openai.describe_file, bytes(raw), msg.document.file_name)
                await msg.reply_text(f"📄 Файл: {msg.document.file_name}\n\n{desc}")
            elif msg.photo:
                f = await msg.photo[-1].get_file()
                raw = await f.download_as_bytearray()
                desc = await asyncio.to_thread(self.openai.describe_image, bytes(raw))
                await msg.reply_text(f"🖼️ Фото: анализ выполнен.\n\n{desc}")
        except Exception as e:
            logger.exception("analyze attachment failed: %s", e)
            await msg.reply_text(f"Не удалось проанализировать вложение: {e}")

    # ===== CallbackQuery ======================================================
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not q:
            return
        await q.answer()
        data = q.data or ""
        user_id = update.effective_user.id
        st = self._ensure_dialog(user_id)

        # model:<name>
        if data.startswith("model:"):
            st.model = data.split(":", 1)[1]
            await q.edit_message_text(f"Модель установлена: {st.model}")
            return

        # mode:<name>
        if data.startswith("mode:"):
            st.style = data.split(":", 1)[1]
            await q.edit_message_text(f"Стиль установлен: {st.style}")
            return

        # dialogs
        if data == "newdlg":
            dlg_id = self._next_dialog_id
            self._next_dialog_id += 1
            self._dialogs_by_user.setdefault(user_id, {})[dlg_id] = DialogState(dialog_id=dlg_id)
            self._current_dialog_by_user[user_id] = dlg_id
            await q.edit_message_text("Создан новый диалог.")
            return
        if data.startswith("open:"):
            dlg_id = int(data.split(":", 1)[1])
            if dlg_id in self._dialogs_by_user.get(user_id, {}):
                self._current_dialog_by_user[user_id] = dlg_id
                await q.edit_message_text(f"Открыт диалог #{dlg_id}.")
            else:
                await q.edit_message_text("Диалог не найден.")
            return
        if data.startswith("del:"):
            dlg_id = int(data.split(":", 1)[1])
            if dlg_id in self._dialogs_by_user.get(user_id, {}):
                del self._dialogs_by_user[user_id][dlg_id]
                if self._current_dialog_by_user.get(user_id) == dlg_id:
                    rest = list(self._dialogs_by_user.get(user_id, {}).keys())
                    self._current_dialog_by_user[user_id] = rest[0] if rest else None
                await q.edit_message_text(f"Диалог #{dlg_id} удалён.")
            else:
                await q.edit_message_text("Диалог не найден.")
            return

        # KB toggles
        if data.startswith("kb:toggle:"):
            doc_id = data.split(":", 2)[2]
            if doc_id in st.kb_selected_ids:
                st.kb_selected_ids.remove(doc_id)
            else:
                st.kb_selected_ids.append(doc_id)
            await q.answer("OK", show_alert=False)
            return
        if data == "kb:done":
            await q.edit_message_text(
                "Выбор документов сохранён. Буду использовать их как контекст при ответах."
            )
            return
