from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
)
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.openai_helper import OpenAIHelper
from bot.settings import Settings

# --- knowledge_base интеграция (мягкая) ---
KB_AVAILABLE = True
KB_MISSING_REASON = ""
try:
    from bot.knowledge_base.indexer import sync as kb_sync  # type: ignore
    from bot.knowledge_base.retriever import list_documents as kb_list_docs  # type: ignore
except Exception as e:  # модуль не в сборке или сломан импорт
    KB_AVAILABLE = False
    KB_MISSING_REASON = (
        "Модуль базы знаний не найден: "
        f"{e}. Убедитесь, что в деплой включены файлы:\n"
        "  - bot/knowledge_base/indexer.py\n"
        "  - bot/knowledge_base/retriever.py\n"
        "и их зависимости (yadisk, sqlalchemy, модели БД и т.п.)."
    )

logger = logging.getLogger(__name__)

# ----------------- Память per-chat/диалоги -----------------

@dataclass
class DialogState:
    id: int
    title: str
    created_at: float
    updated_at: float
    selected_doc_ids: List[int] = field(default_factory=list)
    kb_enabled: bool = False


@dataclass
class ChatState:
    dialogs: Dict[int, DialogState] = field(default_factory=dict)
    current_id: Optional[int] = None


def _ts_fmt(ts: float) -> str:
    # компактный штамп времени
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


class ChatGPTTelegramBot:
    """
    Основная логика бота.
    """

    STYLES = {
        "pro": "Профессиональный — кратко, по делу, максимально точно и уверенно.",
        "expert": "Экспертный — глубокая аргументация, упоминание подходов/терминов.",
        "user": "Пользовательский — простой язык, без перегруза терминами.",
        "ceo": "СЕО — стратегический взгляд владельца бизнеса (EMBA/DBA).",
    }

    def __init__(self, openai: OpenAIHelper, settings: Settings) -> None:
        self.openai = openai
        self.settings = settings
        self.state: Dict[int, ChatState] = {}

    # ------------- Вспомогательные -------------

    def _get_chat(self, chat_id: int) -> ChatState:
        st = self.state.get(chat_id)
        if not st:
            st = ChatState()
            self.state[chat_id] = st
        return st

    def _ensure_current_dialog(self, chat_id: int) -> DialogState:
        st = self._get_chat(chat_id)
        if st.current_id is None or st.current_id not in st.dialogs:
            dlg_id = int(time.time() * 1000)  # уникальнее
            st.dialogs[dlg_id] = DialogState(
                id=dlg_id,
                title="Диалог",
                created_at=time.time(),
                updated_at=time.time(),
            )
            st.current_id = dlg_id
        return st.dialogs[st.current_id]

    def _typing_once(self, update: Update, context: ContextTypes.DEFAULT_TYPE, seconds: float = 0.6):
        return context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    def _kb_snippets_from_ids(self, ids: List[int]) -> Optional[str]:
        if not ids:
            return None
        if not KB_AVAILABLE or not kb_list_docs:
            return "\n".join(f"- документ #{i}" for i in ids)
        docs = {d.id: d for d in (kb_list_docs() or [])}
        titles = [f"- {docs[i].title}" for i in ids if i in docs]
        return "\n".join(titles) if titles else None

    def _auto_title(self, old: str, user_text: str) -> str:
        """ Простая авто-генерация краткого названия по первому сообщению. """
        if old != "Диалог":
            return old
        t = user_text.strip().splitlines()[0][:40]
        return t if t else old

    # ---------------- Установка команд/хендлеров ----------------

    def install(self, app: Application) -> None:
        # команды
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
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

        # кнопки
        app.add_handler(CallbackQueryHandler(self.on_model_select, pattern=r"^model:"))
        app.add_handler(CallbackQueryHandler(self.on_mode_select, pattern=r"^mode:"))
        if KB_AVAILABLE:
            app.add_handler(CallbackQueryHandler(self.on_kb_toggle, pattern=r"^kb:"))
        app.add_handler(CallbackQueryHandler(self.on_dialog_action, pattern=r"^dlg:"))

        # сообщения
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file_or_photo))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.on_text))

        # глобальный error handler — ловим в т.ч. Conflict
        app.add_error_handler(self.on_error)

        async def _set_global_commands(application: Application):
            try:
                commands = [
                    ("start", "Запуск/справка"),
                    ("help", "Справка"),
                    ("reset", "Сброс контекста"),
                    ("stats", "Статистика"),
                    ("model", "Выбор модели"),
                    ("mode", "Стиль ответов"),
                    ("dialogs", "Диалоги"),
                    ("del", "Удалить текущий диалог"),
                    ("img", "Сгенерировать изображение"),
                    ("web", "Веб-поиск"),
                ]
                if KB_AVAILABLE:
                    # /kb добавляем только если модуль реально доступен
                    commands.insert(4, ("kb", "База знаний"))
                await application.bot.set_my_commands(commands)
            except Exception as e:
                logger.warning("Не удалось установить команды: %s", e)

        # post_init — атрибут, а не метод
        if getattr(app, "post_init", None) is None:
            app.post_init = _set_global_commands
        else:
            prev = app.post_init
            async def _chain(a: Application):
                if prev:
                    await prev(a)
                await _set_global_commands(a)
            app.post_init = _chain

    # ---------------- Error handler ----------------

    async def on_error(self, update: Optional[Update], context: ContextTypes.DEFAULT_TYPE):
        err = context.error
        if isinstance(err, Conflict):
            # Частая ситуация: второй инстанс бота запущен параллельно (локально/на другом сервисе).
            logger.warning("Polling conflict detected: another instance of the bot is running. "
                           "Этот инстанс продолжит работу; проверьте, что не запущена копия локально/в другом окружении.")
            # Если хотите, чтобы «лишний» инстанс сам останавливался:
            # await context.application.stop()
            return
        # иначе — стандартный лог
        logger.exception("Unhandled error in handler: %s", err)

    # ---------------- Команды ----------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        base = [
            "/help — справка",
            "/reset — сброс контекста",
            "/stats — статистика",
            "/model — выбор модели",
            "/mode — стиль ответов",
            "/dialogs — список диалогов (открыть/удалить)",
            "/del — удалить текущий диалог",
            "/img <описание> — генерация изображения",
            "/web <запрос> — веб‑поиск со ссылками",
        ]
        if KB_AVAILABLE:
            base.insert(3, "/kb — база знаний (включить/исключить документы)")
        await update.message.reply_text("Привет! Я готов к работе.\nКоманды:\n" + "\n".join(base))

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        lines = [
            "/reset — сброс контекста",
            "/stats — статистика",
        ]
        if KB_AVAILABLE:
            lines.append("/kb — база знаний (включить/исключить документы)")
        lines.extend([
            "/model — выбор модели OpenAI",
            "/mode — стиль ответов (Pro/Expert/User/CEO)",
            "/dialogs — список диалогов (открыть/удалить)",
            "/del — удалить текущий диалог",
            "/img <описание> — сгенерировать изображение",
            "/web <запрос> — веб‑поиск со ссылками",
        ])
        if not KB_AVAILABLE:
            lines.append("\n⚠️ База знаний недоступна: модуль не включён в сборку.\n" + KB_MISSING_REASON)
        await update.message.reply_text("\n".join(lines))

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        st = self._get_chat(chat_id)
        dlg_id = int(time.time() * 1000)
        st.dialogs[dlg_id] = DialogState(
            id=dlg_id,
            title="Диалог",
            created_at=time.time(),
            updated_at=time.time(),
        )
        st.current_id = dlg_id
        await update.message.reply_text("🔄 Новый диалог создан. Контекст очищен.")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        dlg = self._ensure_current_dialog(chat_id)
        model = self.openai.get_current_model(chat_id)
        style = self.openai.get_style(chat_id)
        kb = "включена" if dlg.kb_enabled else "выключена"

        # названия выбранных документов, если есть knowledge_base
        doc_titles: List[str] = []
        if KB_AVAILABLE and kb_list_docs and dlg.selected_doc_ids:
            try:
                docs = {d.id: d for d in kb_list_docs() or []}
                for d_id in dlg.selected_doc_ids:
                    if d_id in docs:
                        doc_titles.append(docs[d_id].title)
            except Exception:
                pass

        lines = [
            "📊 Статистика:",
            f"- Диалог: {dlg.title}",
            f"- Модель: {model}",
            f"- Стиль: {style.capitalize()}",
            f"- База знаний: {kb}" + ("" if KB_AVAILABLE else " (модуль недоступен)"),
            f"- Документов выбрано: {len(dlg.selected_doc_ids)}",
            f"- Создан: {_ts_fmt(dlg.created_at)} • Обновлён: {_ts_fmt(dlg.updated_at)}",
        ]
        if doc_titles:
            lines.append(f"- В контексте: {', '.join(doc_titles)}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        models, current = self.openai.list_models_with_current(chat_id)
        if not models:
            await update.message.reply_text("Список моделей недоступен.")
            return
        rows = []
        for m in models:
            mark = " ✅" if m == current else ""
            rows.append([InlineKeyboardButton(text=f"{m}{mark}", callback_data=f"model:set:{m}")])
        await update.message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(rows))

    async def on_model_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        _, action, model = q.data.split(":", 2)
        if action != "set":
            return
        chat_id = update.effective_chat.id
        self.openai.set_current_model(chat_id, model)
        await q.edit_message_text(f"Модель установлена: {model}")

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        current = self.openai.get_style(chat_id)
        rows = []
        for key, desc in self.STYLES.items():
            mark = " ✅" if key == current else ""
            rows.append([InlineKeyboardButton(f"{key}{mark}", callback_data=f"mode:set:{key}")])
        await update.message.reply_text("Выберите стиль:", reply_markup=InlineKeyboardMarkup(rows))

    async def on_mode_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        _, action, style = q.data.split(":", 2)
        if action != "set":
            return
        chat_id = update.effective_chat.id
        self.openai.set_style(chat_id, style)
        await q.edit_message_text(f"Стиль установлен: {style}")

    async def cmd_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not KB_AVAILABLE or not kb_sync or not kb_list_docs:
            await update.message.reply_text(
                "⚠️ База знаний недоступна в этой сборке.\n" + KB_MISSING_REASON
            )
            return

        chat_id = update.effective_chat.id
        dlg = self._ensure_current_dialog(chat_id)

        msg = await update.message.reply_text("⏳ Синхронизирую базу знаний...")
        added = updated = deleted = 0
        try:
            res = kb_sync()
            if isinstance(res, tuple) and len(res) >= 3:
                added, updated, deleted = res[:3]
            elif isinstance(res, dict):
                added = int(res.get("added", 0))
                updated = int(res.get("updated", 0))
                deleted = int(res.get("deleted", 0))
        except Exception as e:
            await msg.edit_text(f"Ошибка синхронизации: {e}")
            return

        try:
            docs = kb_list_docs() or []
        except Exception as e:
            await msg.edit_text(f"Синхронизация ок. Ошибка чтения списка: {e}")
            return

        kb_switch = "🔓 Включить БЗ" if not dlg.kb_enabled else "🔒 Выключить БЗ"
        rows: List[List[InlineKeyboardButton]] = [
            [InlineKeyboardButton(kb_switch, callback_data="kb:switch")],
        ]
        for d in docs:
            on = "✅" if d.id in dlg.selected_doc_ids else "❌"
            rows.append([InlineKeyboardButton(f"{on} {d.title}", callback_data=f"kb:toggle:{d.id}")])

        text = (
            f"📚 База знаний:\n"
            f"— Добавлено: {added}, Обновлено: {updated}, Удалено: {deleted}\n"
            f"Выберите документы для контекста."
        )
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))

    async def on_kb_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        parts = q.data.split(":")
        chat_id = update.effective_chat.id
        dlg = self._ensure_current_dialog(chat_id)

        if len(parts) == 2 and parts[1] == "switch":
            dlg.kb_enabled = not dlg.kb_enabled
            await q.answer("БЗ включена" if dlg.kb_enabled else "БЗ выключена")
            return

        if len(parts) == 3 and parts[1] == "toggle":
            try:
                doc_id = int(parts[2])
            except Exception:
                await q.answer("Некорректный id документа.")
                return
            if doc_id in dlg.selected_doc_ids:
                dlg.selected_doc_ids.remove(doc_id)
            else:
                dlg.selected_doc_ids.append(doc_id)
            await q.answer("Обновлено")

    async def cmd_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        st = self._get_chat(chat_id)
        if not st.dialogs:
            self._ensure_current_dialog(chat_id)

        rows: List[List[InlineKeyboardButton]] = []
        items = sorted(st.dialogs.values(), key=lambda d: d.updated_at, reverse=True)
        for d in items:
            mark = " 🟢" if d.id == st.current_id else ""
            title = f"{d.title}{mark}\nсозд: {_ts_fmt(d.created_at)} • изм: {_ts_fmt(d.updated_at)}"
            rows.append([
                InlineKeyboardButton(f"↪️ {title}", callback_data=f"dlg:open:{d.id}"),
                InlineKeyboardButton("🗑 Удалить", callback_data=f"dlg:del:{d.id}"),
            ])

        await update.message.reply_text("Диалоги:", reply_markup=InlineKeyboardMarkup(rows))

    async def on_dialog_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        _, action, ident = q.data.split(":", 2)
        chat_id = update.effective_chat.id
        st = self._get_chat(chat_id)

        try:
            dlg_id = int(ident)
        except Exception:
            await q.answer("Некорректный идентификатор диалога.")
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
            await update.message.reply_text("Диалог удалён. Начинаю новый.")
        else:
            await update.message.reply_text("Текущий диалог не найден.")

    async def cmd_img(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /img <описание>")
            return
        prompt = " ".join(context.args)
        chat_id = update.effective_chat.id

        await self._typing_once(update, context)
        try:
            res = self.openai.generate_image(prompt)
            bio = io.BytesIO(res.image_bytes)
            bio.name = "image.png"
            bio.seek(0)
            cap = f"🖼️ Итоговый промпт:\n{res.final_prompt}\n\nМодель: {res.model_used}"
            await update.message.reply_photo(photo=InputFile(bio), caption=cap)
        except Exception as e:
            logger.error("Image generation failed: %s", e, exc_info=True)
            await update.message.reply_text(f"Ошибка генерации изображения: {e}")

    async def cmd_web(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /web <запрос>")
            return
        query = " ".join(context.args)
        await self._typing_once(update, context)
        try:
            results = self.openai.web_search(query, limit=5)
        except Exception as e:
            await update.message.reply_text(f"Ошибка веб‑поиска: {e}")
            return
        if not results:
            await update.message.reply_text("Не удалось найти источники.")
            return
        lines = [f"🔎 Поиск: {query}", ""]
        for i, r in enumerate(results, 1):
            title = r.get("title") or r.get("url")
            url = r.get("url")
            snippet = r.get("snippet", "")
            lines.append(f"{i}. {title}\n{url}")
            if snippet:
                lines.append(snippet)
            lines.append("")
        await update.message.reply_text("\n".join(lines).strip())

    # --------------- Сообщения ----------------

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        dlg = self._ensure_current_dialog(chat_id)
        dlg.title = self._auto_title(dlg.title, update.message.text)
        dlg.updated_at = time.time()

        await self._typing_once(update, context)
        kb_snip = None
        if dlg.kb_enabled and dlg.selected_doc_ids:
            kb_snip = self._kb_snippets_from_ids(dlg.selected_doc_ids)

        try:
            reply = self.openai.chat(chat_id, update.message.text, kb_snip)
        except Exception as e:
            logger.error("Chat failed: %s", e, exc_info=True)
            reply = f"Ошибка обращения к OpenAI: {e}"

        await update.message.reply_text(reply)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        dlg = self._ensure_current_dialog(chat_id)
        dlg.updated_at = time.time()

        file_id = update.message.voice.file_id
        f = await context.bot.get_file(file_id)
        bx = httpx.get(f.file_path, timeout=60.0)
        bx.raise_for_status()

        await self._typing_once(update, context)
        try:
            text = self.openai.transcribe(bx.content, filename_hint="audio.ogg")
            await update.message.reply_text(f"🎙️ Вы сказали: {text}")
        except Exception as e:
            await update.message.reply_text(f"Ошибка распознавания: {e}")
            return

        kb_snip = None
        if dlg.kb_enabled and dlg.selected_doc_ids:
            kb_snip = self._kb_snippets_from_ids(dlg.selected_doc_ids)

        try:
            reply = self.openai.chat(chat_id, text, kb_snip)
        except Exception as e:
            reply = f"Ошибка обращения к OpenAI: {e}"

        await update.message.reply_text(reply)

    async def on_file_or_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        При получении файлов — **не** добавляем сразу в БЗ.
        Короткий анализ и предложение использовать /kb для добавления/выбора.
        """
        msg = update.message
        if msg.document:
            name = msg.document.file_name or "документ"
            size = msg.document.file_size or 0
            await msg.reply_text(
                f"Получен файл: {name} ({size} байт).\n"
                f"Я не добавляю файлы автоматически в БЗ.\n"
                f"Используйте /kb для синхронизации и выбора документов."
            )
            return
        if msg.photo:
            await msg.reply_text(
                "Получено изображение. Анализ возможен по запросу. "
                "Добавление в БЗ доступно только для документов. Используйте /kb."
            )
            return
