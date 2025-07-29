# bot/telegram_bot.py
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators,
    InputFile,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from .openai_helper import OpenAIHelper
from .settings import Settings

logger = logging.getLogger(__name__)

# --- Опциональная База Знаний ------------------------------------------------
KB_AVAILABLE = False
KB_MISSING_REASON = "Папка bot/knowledge_base/* отсутствует в сборке."

try:
    from .knowledge_base.context_manager import KBContextManager  # type: ignore
    from .knowledge_base.indexer import KnowledgeBaseIndexer  # type: ignore
    KB_AVAILABLE = True
    KB_MISSING_REASON = ""
except Exception as e:
    KB_AVAILABLE = False
    KB_MISSING_REASON = f"Модуль БЗ недоступен: {e!s}"

# --- Состояние диалога/чата ---------------------------------------------------

def _ts_fmt(ts: float) -> str:
    # короткая дата/время: 29.07 14:35
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(ts)
    return dt.strftime("%d.%m %H:%M")

@dataclass
class DialogState:
    id: int
    title: str = "Диалог"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    kb_enabled: bool = False
    kb_selected: List[int] = field(default_factory=list)  # ids документов БЗ
    style: str = "Pro"  # Pro | Expert | User | CEO
    model: Optional[str] = None  # модель OpenAI на уровне диалога

@dataclass
class ChatState:
    user_id: int
    dialogs: Dict[int, DialogState] = field(default_factory=dict)
    current_id: Optional[int] = None

# --- Основной класс бота ------------------------------------------------------

class ChatGPTTelegramBot:
    def __init__(self, openai: OpenAIHelper, settings: Settings) -> None:
        self.openai = openai
        self.settings = settings

        self.allowed: set[int] = set(getattr(settings, "allowed_user_ids", []) or [])
        self.admins: set[int] = set(getattr(settings, "admin_user_ids", []) or [])
        self.state: Dict[int, ChatState] = {}

        # Инициализация БЗ при наличии
        self.kb_indexer: Optional[KnowledgeBaseIndexer] = None
        self.kb_ctx: Optional[KBContextManager] = None
        if KB_AVAILABLE:
            try:
                self.kb_indexer = KnowledgeBaseIndexer(settings)
                self.kb_ctx = KBContextManager(settings)
            except Exception as e:
                logger.warning("Не удалось инициализировать БЗ: %s", e)

    # ------------------------- ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ -----------------------

    def _get_chat(self, chat_id: int) -> ChatState:
        if chat_id not in self.state:
            self.state[chat_id] = ChatState(user_id=chat_id)
        st = self.state[chat_id]
        if st.current_id is None:
            self._ensure_current_dialog(chat_id)
        return st

    def _ensure_current_dialog(self, chat_id: int) -> None:
        st = self.state[chat_id]
        if st.current_id is None:
            dlg_id = int(time.time() * 1000)
            st.dialogs[dlg_id] = DialogState(id=dlg_id)
            st.current_id = dlg_id

    async def _typing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    def _build_commands(self) -> List[BotCommand]:
        cmds = [
            BotCommand("start", "Запуск/справка"),
            BotCommand("help", "Справка"),
            BotCommand("new", "Новый диалог"),
            BotCommand("reset", "Сброс контекста"),
            BotCommand("stats", "Статистика"),
            BotCommand("model", "Выбор модели"),
            BotCommand("mode", "Стиль ответов"),
            BotCommand("dialogs", "Диалоги"),
            BotCommand("del", "Удалить текущий диалог"),
            BotCommand("img", "Сгенерировать изображение"),
            BotCommand("web", "Веб‑поиск"),
        ]
        if KB_AVAILABLE:
            cmds.insert(6, BotCommand("kb", "База знаний"))
        return cmds

    async def _refresh_all_scopes(self, bot):
        scopes = [
            BotCommandScopeDefault(),
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
        ]
        commands = self._build_commands()
        for scope in scopes:
            try:
                await bot.delete_my_commands(scope=scope)
            except Exception:
                pass
            await bot.set_my_commands(commands, scope=scope)

            # Дублируем для ru
            try:
                await bot.delete_my_commands(scope=scope, language_code="ru")
            except Exception:
                pass
            await bot.set_my_commands(commands, scope=scope, language_code="ru")

    # ------------------------------ РЕГИСТРАЦИЯ -----------------------------

    def install(self, app: Application) -> None:
        # Команды
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("new", self.cmd_new))
        app.add_handler(CommandHandler("reset", self.cmd_reset))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        if KB_AVAILABLE:
            app.add_handler(CommandHandler("kb", self.cmd_kb))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("dialogs", self.cmd_dialogs))
        app.add_handler(CommandHandler("del", self.cmd_del))
        app.add_handler(CommandHandler("img", self.cmd_img))
        app.add_handler(CommandHandler("web", self.cmd_web))
        app.add_handler(CommandHandler("reload_menu", self.cmd_reload_menu))

        # Callback-и
        app.add_handler(CallbackQueryHandler(self.on_model_action, pattern=r"^model:"))
        app.add_handler(CallbackQueryHandler(self.on_mode_action, pattern=r"^mode:"))
        app.add_handler(CallbackQueryHandler(self.on_dialog_action, pattern=r"^dlg:"))
        if KB_AVAILABLE:
            app.add_handler(CallbackQueryHandler(self.on_kb_action, pattern=r"^kb:"))

        # Сообщения
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        # Устанавливаем команды при инициализации
        async def _post_init(application: Application):
            await self._refresh_all_scopes(application.bot)

        app.post_init(_post_init)

    # ------------------------------ КОМАНДЫ ---------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        base = [
            "/help — справка",
            "/new — новый диалог",
            "/reset — сброс контекста",
            "/stats — статистика",
            "/model — выбор модели",
            "/mode — стиль ответов",
            "/dialogs — диалоги (открыть/удалить)",
            "/del — удалить текущий диалог",
            "/img <описание> — генерация изображения",
            "/web <запрос> — веб‑поиск со ссылками",
        ]
        if KB_AVAILABLE:
            base.insert(4, "/kb — база знаний (вкл/искл документы)")
        await update.message.reply_text("Привет! Я готов к работе.\nКоманды:\n" + "\n".join(base))

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        lines = [
            "/new — новый диалог",
            "/reset — сброс контекста",
            "/stats — статистика",
        ]
        if KB_AVAILABLE:
            lines.append("/kb — база знаний (включить/исключить документы)")
        lines.extend([
            "/model — выбор модели OpenAI",
            "/mode — стиль ответов (Pro/Expert/User/CEO)",
            "/dialogs — диалоги (открыть/удалить)",
            "/del — удалить текущий диалог",
            "/img <описание> — сгенерировать изображение",
            "/web <запрос> — веб‑поиск со ссылками",
        ])
        await update.message.reply_text("\n".join(lines))

    async def cmd_reload_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._refresh_all_scopes(context.application.bot)
        await update.message.reply_text("✅ Меню команд обновлено для всех scope.")

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        st = self._get_chat(chat_id)
        dlg_id = int(time.time() * 1000)
        st.dialogs[dlg_id] = DialogState(
            id=dlg_id, title="Диалог", created_at=time.time(), updated_at=time.time()
        )
        st.current_id = dlg_id
        await update.message.reply_text("🆕 Создан и выбран новый диалог. Можете писать сообщение.")

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.state.pop(chat_id, None)
        await update.message.reply_text("🔄 Новый диалог создан. Контекст очищен.")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._get_chat(update.effective_chat.id)
        dlg = st.dialogs.get(st.current_id)
        model = dlg.model or self.openai.get_user_model(st.user_id) or self.openai.default_model
        style = dlg.style
        kb_on = "включена" if (dlg.kb_enabled if dlg else False) else "выключена"
        kb_cnt = len(dlg.kb_selected) if dlg else 0
        title = dlg.title if dlg else "—"
        msg = (
            "📊 Статистика:\n"
            f"- Диалог: {title}\n"
            f"- Модель: {model}\n"
            f"- Стиль: {style}\n"
            f"- База знаний: {kb_on}\n"
            f"- Документов выбрано: {kb_cnt}"
        )
        await update.message.reply_text(msg)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показ списка моделей с пометкой активной."""
        user_id = update.effective_user.id
        st = self._get_chat(update.effective_chat.id)
        dlg = st.dialogs.get(st.current_id)
        current = dlg.model or self.openai.get_user_model(user_id) or self.openai.default_model

        models = self.openai.list_models_for_user(user_id)
        rows: List[List[InlineKeyboardButton]] = []
        for m in models:
            mark = " ✅" if m == current else ""
            rows.append([InlineKeyboardButton(m + mark, callback_data=f"model:set:{m}")])
        await update.message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(rows))

    async def on_model_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        parts = q.data.split(":", 2)
        if len(parts) < 3:
            return
        _, action, model = parts
        if action != "set":
            return
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        st = self._get_chat(chat_id)
        dlg = st.dialogs.get(st.current_id)
        # сохраняем на уровне диалога, а также user default
        if dlg:
            dlg.model = model
            dlg.updated_at = time.time()
        self.openai.set_user_model(user_id, model)
        await q.edit_message_text(f"Модель переключена на: {model}")

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        st = self._get_chat(update.effective_chat.id)
        dlg = st.dialogs.get(st.current_id)
        current = dlg.style if dlg else "Pro"
        options = ["Pro", "Expert", "User", "CEO"]
        rows = []
        for opt in options:
            mark = " ✅" if opt == current else ""
            rows.append([InlineKeyboardButton(opt + mark, callback_data=f"mode:set:{opt}")])
        await update.message.reply_text("Выберите стиль ответов:", reply_markup=InlineKeyboardMarkup(rows))

    async def on_mode_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        parts = q.data.split(":", 2)
        if len(parts) < 3:
            return
        _, action, style = parts
        if action != "set":
            return
        st = self._get_chat(update.effective_chat.id)
        dlg = st.dialogs.get(st.current_id)
        if dlg:
            dlg.style = style
            dlg.updated_at = time.time()
        await q.edit_message_text(f"Стиль переключён на: {style}")

    async def cmd_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        st = self._get_chat(chat_id)
        if not st.dialogs:
            self._ensure_current_dialog(chat_id)
        rows: List[List[InlineKeyboardButton]] = []

        # Первая строка — создать новый
        rows.append([InlineKeyboardButton("➕ Новый диалог", callback_data="dlg:new")])

        items = sorted(st.dialogs.values(), key=lambda d: d.updated_at, reverse=True)
        for d in items:
            mark = " 🟢" if d.id == st.current_id else ""
            title = f"{d.title}{mark}\nсозд: {_ts_fmt(d.created_at)} • изм: {_ts_fmt(d.updated_at)}"
            rows.append([InlineKeyboardButton(f"↪️ {title}", callback_data=f"dlg:open:{d.id}")])
            rows.append([InlineKeyboardButton("✖ Удалить", callback_data=f"dlg:del:{d.id}")])

        await update.message.reply_text("Диалоги:", reply_markup=InlineKeyboardMarkup(rows))

    async def on_dialog_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        parts = q.data.split(":", 2)
        if len(parts) < 2:
            return
        action = parts[1]
        chat_id = update.effective_chat.id
        st = self._get_chat(chat_id)

        if action == "new":
            dlg_id = int(time.time() * 1000)
            st.dialogs[dlg_id] = DialogState(id=dlg_id, created_at=time.time(), updated_at=time.time())
            st.current_id = dlg_id
            await q.edit_message_text("🆕 Создан и выбран новый диалог.")
            return

        if len(parts) < 3:
            await q.edit_message_text("Некорректные данные.")
            return

        try:
            dlg_id = int(parts[2])
        except Exception:
            await q.edit_message_text("Некорректный идентификатор диалога.")
            return

        if action == "open":
            if dlg_id in st.dialogs:
                st.current_id = dlg_id
                await q.edit_message_text(f"Открыт диалог: {st.dialogs[dlg_id].title}")
            else:
                await q.edit_message_text("Диалог не найден.")
            return

        if action == "del":
            if dlg_id in st.dialogs:
                del st.dialogs[dlg_id]
                if st.current_id == dlg_id:
                    st.current_id = None
                await q.edit_message_text("Диалог удалён.")
            else:
                await q.edit_message_text("Диалог не найден.")
            return

    async def cmd_del(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        st = self._get_chat(chat_id)
        if st.current_id and st.current_id in st.dialogs:
            del st.dialogs[st.current_id]
            st.current_id = None
            await update.message.reply_text("🗑 Текущий диалог удалён.")
        else:
            await update.message.reply_text("Нет активного диалога.")

    async def cmd_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not KB_AVAILABLE or not self.kb_ctx:
            await update.message.reply_text(f"Модуль базы знаний недоступен. {KB_MISSING_REASON}")
            return

        chat_id = update.effective_chat.id
        st = self._get_chat(chat_id)
        dlg = st.dialogs.get(st.current_id)
        if not dlg:
            self._ensure_current_dialog(chat_id)
            dlg = st.dialogs[st.current_id]

        # По клику в /kb типично синхронизируем индекс (по требованию)
        try:
            if self.kb_indexer:
                added, updated, deleted, unchanged = self.kb_indexer.sync()
                logger.info("KB sync: added=%s, updated=%s, deleted=%s, unchanged=%s",
                            added, updated, deleted, unchanged)
        except Exception as e:
            logger.warning("Ошибка синхронизации БЗ: %s", e)

        # Покажем простое меню вкл/выкл + выбор документов
        rows = [
            [InlineKeyboardButton(
                ("🔌 Отключить БЗ" if dlg.kb_enabled else "⚡ Включить БЗ"),
                callback_data="kb:toggle"
            )],
            [InlineKeyboardButton("📄 Выбрать документы", callback_data="kb:pick")]
        ]
        await update.message.reply_text(
            f"База знаний сейчас: {'включена' if dlg.kb_enabled else 'выключена'}.\n"
            "Вы можете включить/выключить и выбрать документы для контекста.",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    async def on_kb_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not KB_AVAILABLE or not self.kb_ctx:
            await update.callback_query.edit_message_text(f"Модуль БЗ недоступен. {KB_MISSING_REASON}")
            return

        q = update.callback_query
        await q.answer()
        chat_id = update.effective_chat.id
        st = self._get_chat(chat_id)
        dlg = st.dialogs.get(st.current_id)
        if not dlg:
            await q.edit_message_text("Нет активного диалога.")
            return

        parts = q.data.split(":", 1)
        action = parts[1] if len(parts) > 1 else ""

        if action == "toggle":
            dlg.kb_enabled = not dlg.kb_enabled
            dlg.updated_at = time.time()
            await q.edit_message_text(f"База знаний теперь: {'включена' if dlg.kb_enabled else 'выключена'}.")
            return

        if action == "pick":
            # Покажем первые N документов (реализация зависит от KBContextManager)
            try:
                docs = self.kb_ctx.list_documents()
            except Exception as e:
                await q.edit_message_text(f"Ошибка получения списка документов: {e}")
                return

            rows = []
            for d in docs[:50]:  # ограничим клавиатуру
                checked = "✅ " if d.id in dlg.kb_selected else ""
                rows.append([InlineKeyboardButton(f"{checked}{d.title}", callback_data=f"kb:toggle_doc:{d.id}")])
            rows.append([InlineKeyboardButton("Готово", callback_data="kb:done")])
            await q.edit_message_text("Выберите документы:", reply_markup=InlineKeyboardMarkup(rows))
            return

        if action.startswith("toggle_doc:"):
            try:
                doc_id = int(action.split(":", 1)[1])
            except Exception:
                return
            if doc_id in dlg.kb_selected:
                dlg.kb_selected.remove(doc_id)
            else:
                dlg.kb_selected.append(doc_id)
            # Обновим текст клавиатуры, не меняя сообщение
            try:
                docs = self.kb_ctx.list_documents()
                rows = []
                for d in docs[:50]:
                    checked = "✅ " if d.id in dlg.kb_selected else ""
                    rows.append([InlineKeyboardButton(f"{checked}{d.title}", callback_data=f"kb:toggle_doc:{d.id}")])
                rows.append([InlineKeyboardButton("Готово", callback_data="kb:done")])
                await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
            except Exception:
                pass
            return

        if action == "done":
            await q.edit_message_text(f"Выбрано документов: {len(dlg.kb_selected)}. "
                                      f"БЗ {'вкл' if dlg.kb_enabled else 'выкл'}.")
            return

    async def cmd_img(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /img <описание>")
            return
        prompt = " ".join(context.args).strip()
        await self._typing(update, context)

        # модель берём из env/настроек помощника
        model = self.openai.get_image_model()
        try:
            img_bytes, used_prompt = await asyncio.to_thread(self.openai.generate_image, prompt, model=model)
        except Exception as e:
            logger.exception("Image generation failed: %s", e)
            await update.message.reply_text(f"Ошибка генерации изображения: {e}")
            return

        caption = f"🖼️ Сгенерировано.\nМодель: {model}\nПромпт: {used_prompt}"
        await update.message.reply_photo(photo=img_bytes, caption=caption)

    async def cmd_web(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /web <запрос>")
            return
        query = " ".join(context.args).strip()
        await self._typing(update, context)
        try:
            answer, links = await asyncio.to_thread(self.openai.web_search, query)
            if links:
                links_text = "\n".join(f"• {u}" for u in links[:8])
                await update.message.reply_text(f"{answer}\n\n🔗 Источники:\n{links_text}")
            else:
                await update.message.reply_text(f"{answer}\n\n⚠️ Модель не вернула явных ссылок.")
        except Exception as e:
            await update.message.reply_text(f"Ошибка веб‑поиска: {e}")

    # ------------------------------ ОБРАБОТЧИКИ СООБЩЕНИЙ -------------------

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._typing(update, context)
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        text = update.message.text.strip()

        st = self._get_chat(chat_id)
        dlg = st.dialogs.get(st.current_id)
        if not dlg:
            self._ensure_current_dialog(chat_id)
            dlg = st.dialogs[st.current_id]

        # авто‑название из первого сообщения
        if dlg.title == "Диалог" and text:
            dlg.title = (text[:40] + "…") if len(text) > 40 else text

        model = dlg.model or self.openai.get_user_model(user_id) or self.openai.default_model
        style = dlg.style

        kb_docs: List[Tuple[int, str]] = []
        if KB_AVAILABLE and dlg.kb_enabled and dlg.kb_selected and self.kb_ctx:
            try:
                kb_docs = self.kb_ctx.fetch_chunks(dlg.kb_selected, top_k=int(os.getenv("RAG_TOP_K", "8")))
            except Exception as e:
                logger.warning("KB fetch failed: %s", e)

        try:
            reply = await asyncio.to_thread(
                self.openai.chat,
                user_id,
                dlg.id,
                text,
                model=model,
                style=style,
                kb=kb_docs,
            )
        except Exception as e:
            logger.exception("Chat failed: %s", e)
            await update.message.reply_text(f"Ошибка обращения к OpenAI: {e}")
            return

        dlg.updated_at = time.time()
        await update.message.reply_text(reply)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_AUDIO)
        file = await update.effective_message.voice.get_file() if update.effective_message.voice else await update.effective_message.audio.get_file()
        tmp_path = f"/tmp/{file.file_unique_id}.ogg"
        await file.download_to_drive(custom_path=tmp_path)

        try:
            transcript = await asyncio.to_thread(self.openai.transcribe, tmp_path)
        except Exception as e:
            await update.message.reply_text(f"Не удалось распознать аудио: {e}")
            return
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        # Покажем, что было распознано, и ответим текстом
        await self._typing(update, context)
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        st = self._get_chat(chat_id)
        dlg = st.dialogs.get(st.current_id)
        if not dlg:
            self._ensure_current_dialog(chat_id)
            dlg = st.dialogs[st.current_id]

        model = dlg.model or self.openai.get_user_model(user_id) or self.openai.default_model
        style = dlg.style

        kb_docs: List[Tuple[int, str]] = []
        if KB_AVAILABLE and dlg.kb_enabled and dlg.kb_selected and self.kb_ctx:
            try:
                kb_docs = self.kb_ctx.fetch_chunks(dlg.kb_selected, top_k=int(os.getenv("RAG_TOP_K", "8")))
            except Exception as e:
                logger.warning("KB fetch failed: %s", e)

        try:
            reply = await asyncio.to_thread(
                self.openai.chat,
                user_id,
                dlg.id,
                transcript,
                model=model,
                style=style,
                kb=kb_docs,
            )
        except Exception as e:
            await update.message.reply_text(f"Ошибка обращения к OpenAI: {e}")
            return

        dlg.updated_at = time.time()
        await update.message.reply_text(f"🎙️ Вы сказали: {transcript}\n\n{reply}")

    async def on_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Фото/документы НЕ добавляем в БЗ автоматически. Делаем краткий анализ."""
        await self._typing(update, context)
        msg = update.effective_message

        # Скачаем файл во временную папку
        if msg.photo:
            t = msg.photo[-1]
            f = await t.get_file()
            ext = ".jpg"
        else:
            doc = msg.document
            if not doc:
                return
            f = await doc.get_file()
            filename = doc.file_name or f.file_unique_id
            _, ext = os.path.splitext(filename)
            if not ext:
                ext = ".bin"

        tmp_path = f"/tmp/{f.file_unique_id}{ext}"
        await f.download_to_drive(custom_path=tmp_path)

        try:
            summary = await asyncio.to_thread(self.openai.describe_file, tmp_path)
        except Exception as e:
            summary = f"Не удалось проанализировать файл: {e}"
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        tail = ""
        if KB_AVAILABLE and (update.effective_user.id in self.admins):
            tail = "\n\nЧтобы добавить в базу знаний, используйте команду /kb и выберите документ."
        await update.message.reply_text(f"📎 Файл получен.\nКраткий разбор:\n{summary}{tail}")
