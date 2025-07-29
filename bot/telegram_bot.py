from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
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
try:
    from bot.knowledge_base.indexer import sync as kb_sync  # type: ignore
    from bot.knowledge_base.retriever import list_documents as kb_list_docs  # -> List[Document-like] # type: ignore
    # ожидаем у Document поля: id, title
except Exception:
    KB_AVAILABLE = False
    kb_sync = None
    kb_list_docs = None

logger = logging.getLogger(__name__)

# ----------------- Память per-chat -----------------

@dataclass
class ChatState:
    selected_doc_ids: List[int] = field(default_factory=list)  # документы БЗ в контексте
    kb_enabled: bool = False  # включена ли БЗ
    # Имя диалога (для /stats)
    title: str = "Диалог"


class ChatGPTTelegramBot:
    """
    Основная логика бота. Никаких сильных предположений о БД/моделях.
    Храним минимальное per-chat состояние в памяти процесса.
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

        # per-chat состояние
        self.state: Dict[int, ChatState] = {}

    # ------------- Вспомогательные -------------

    def _get_state(self, chat_id: int) -> ChatState:
        st = self.state.get(chat_id)
        if not st:
            st = ChatState()
            self.state[chat_id] = st
        return st

    async def _typing(self, update: Update, context: ContextTypes.DEFAULT_TYPE, seconds: float = 1.0):
        """
        Классический индикатор набора: разово отправляем "typing".
        (Можно сделать петлю каждые 4s, но чаще хватает разового пинга).
        """
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        except Exception:
            pass
        await asyncio.sleep(seconds)

    def _kb_snippets_from_ids(self, doc_ids: List[int]) -> Optional[str]:
        """
        Блок, который подмешиваем в system. Здесь можно сделать извлечение сниппетов.
        Сейчас — просто перечисление названий (без тяжёлого RAG).
        """
        if not KB_AVAILABLE or not kb_list_docs:
            if not doc_ids:
                return None
            titles = [f"- документ #{i}" for i in doc_ids]
            return "\n".join(titles)

        docs = {d.id: d for d in (kb_list_docs() or [])}
        titles = []
        for i in doc_ids:
            d = docs.get(i)
            if d:
                titles.append(f"- {d.title}")
        if not titles:
            return None
        return "\n".join(titles)

    # ---------------- Установка команд ----------------

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
        app.add_handler(CommandHandler("del", self.cmd_del))
        app.add_handler(CommandHandler("img", self.cmd_img))
        app.add_handler(CommandHandler("web", self.cmd_web))

        # Колбэки
        app.add_handler(CallbackQueryHandler(self.on_model_select, pattern=r"^model:"))
        app.add_handler(CallbackQueryHandler(self.on_mode_select, pattern=r"^mode:"))
        app.add_handler(CallbackQueryHandler(self.on_kb_toggle, pattern=r"^kb:"))
        app.add_handler(CallbackQueryHandler(self.on_dialog_action, pattern=r"^dlg:"))

        # Сообщения
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file_or_photo))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.on_text))

        # Меню команд — через post_init (атрибут, а не метод!)
        async def _set_global_commands(application: Application):
            try:
                await application.bot.set_my_commands([
                    ("start", "Запуск/справка"),
                    ("help", "Справка"),
                    ("reset", "Сброс контекста"),
                    ("stats", "Статистика"),
                    ("kb", "База знаний"),
                    ("model", "Выбор модели"),
                    ("mode", "Стиль ответов"),
                    ("dialogs", "Диалоги"),
                    ("del", "Удалить текущий диалог"),
                    ("img", "Сгенерировать изображение"),
                    ("web", "Веб-поиск"),
                ])
            except Exception as e:
                logger.warning("Не удалось установить команды: %s", e)

        if getattr(app, "post_init", None) is None:
            app.post_init = _set_global_commands
        else:
            prev_cb = app.post_init
            async def _chained(application: Application):
                if prev_cb:
                    await prev_cb(application)
                await _set_global_commands(application)
            app.post_init = _chained

    # ---------------- Команды ----------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет! Я готов к работе.\n"
            "Команды: /help, /reset, /stats, /kb, /model, /mode, /dialogs, /del, /img, /web"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "/reset — сброс контекста\n"
            "/stats — статистика\n"
            "/kb — база знаний (включить/исключить документы)\n"
            "/model — выбор модели OpenAI\n"
            "/mode — стиль ответов (Pro/Expert/User/CEO)\n"
            "/dialogs — список диалогов (открыть/удалить)\n"
            "/del — удалить текущий диалог\n"
            "/img <описание> — сгенерировать изображение\n"
            "/web <запрос> — веб‑поиск со ссылками"
        )

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.state.pop(chat_id, None)
        await update.message.reply_text("🔄 Новый диалог создан. Контекст очищен.")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        st = self._get_state(chat_id)
        model = self.openai.get_current_model(chat_id)
        style = self.openai.get_style(chat_id)
        kb = "включена" if st.kb_enabled else "выключена"

        # названия выбранных документов, если есть knowledge_base
        doc_titles: List[str] = []
        if KB_AVAILABLE and kb_list_docs and st.selected_doc_ids:
            docs = {d.id: d for d in kb_list_docs() or []}
            for d_id in st.selected_doc_ids:
                if d_id in docs:
                    doc_titles.append(docs[d_id].title)

        text = [
            "📊 Статистика:",
            f"- Диалог: {st.title}",
            f"- Модель: {model}",
            f"- Стиль: {style.capitalize()}",
            f"- База знаний: {kb}",
            f"- Документов выбрано: {len(st.selected_doc_ids)}",
        ]
        if doc_titles:
            text.append(f"- В контексте: {', '.join(doc_titles)}")

        await update.message.reply_text("\n".join(text))

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
        chat_id = update.effective_chat.id
        st = self._get_state(chat_id)

        msg = await update.message.reply_text("⏳ Синхронизирую базу знаний...")
        added = updated = deleted = 0

        if not KB_AVAILABLE or not kb_sync or not kb_list_docs:
            await msg.edit_text("⚠️ Модуль базы знаний недоступен в этой сборке.")
            return

        try:
            # ожидаем, что kb_sync() вернёт (added, updated, deleted) или dict со счетчиками
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

        docs = []
        try:
            docs = kb_list_docs() or []
        except Exception as e:
            await msg.edit_text(f"Синхронизация ок. Ошибка чтения списка: {e}")
            return

        # Переключатель БЗ
        kb_switch = "🔓 Включить БЗ" if not st.kb_enabled else "🔒 Выключить БЗ"
        rows: List[List[InlineKeyboardButton]] = [
            [InlineKeyboardButton(kb_switch, callback_data="kb:switch")],
        ]

        # Документы
        for d in docs:
            on = "✅" if d.id in st.selected_doc_ids else "❌"
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
        if len(parts) == 2 and parts[1] == "switch":
            chat_id = update.effective_chat.id
            st = self._get_state(chat_id)
            st.kb_enabled = not st.kb_enabled
            await q.answer("БЗ включена" if st.kb_enabled else "БЗ выключена")
            # Обновлять сообщение целиком не будем — чтобы не дёргать DD
            return

        if len(parts) == 3 and parts[1] == "toggle":
            chat_id = update.effective_chat.id
            st = self._get_state(chat_id)
            try:
                doc_id = int(parts[2])
            except Exception:
                await q.answer("Некорректный id документа.")
                return
            if doc_id in st.selected_doc_ids:
                st.selected_doc_ids.remove(doc_id)
            else:
                st.selected_doc_ids.append(doc_id)
            await q.answer("Обновлено")

    async def cmd_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Упрощённый список «диалогов»: в памяти у нас один, поэтому
        показываем текущий title и даём удалить/создать новый.
        Если у вас есть БД со списком, можно заменить реализацией на SQL.
        """
        chat_id = update.effective_chat.id
        st = self._get_state(chat_id)
        rows = [
            [InlineKeyboardButton("Открыть текущий", callback_data="dlg:open:current")],
            [InlineKeyboardButton("Удалить текущий", callback_data="dlg:del:current")],
        ]
        await update.message.reply_text(f"Диалоги (текущий: {st.title})", reply_markup=InlineKeyboardMarkup(rows))

    async def on_dialog_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        _, action, ident = q.data.split(":", 3)
        chat_id = update.effective_chat.id

        if action == "open":
            # У нас единый текущий, просто ответим
            st = self._get_state(chat_id)
            await q.edit_message_text(f"Открыт диалог: {st.title}")
            return

        if action == "del":
            # Сбросим состояние
            self.state.pop(chat_id, None)
            await q.edit_message_text("Диалог удалён. Начинаю новый.")
            return

    async def cmd_del(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.state.pop(chat_id, None)
        await update.message.reply_text("Диалог удалён. Начинаю новый.")

    async def cmd_img(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /img <описание>")
            return
        prompt = " ".join(context.args)
        chat_id = update.effective_chat.id

        await self._typing(update, context, 0.5)
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
        await self._typing(update, context, 0.5)
        try:
            results = self.openai.web_search(query, limit=3)
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
        st = self._get_state(chat_id)

        await self._typing(update, context, 0.5)
        # БЗ-сниппеты по выбранным документам (упрощённо — названия)
        kb_snip = None
        if st.kb_enabled and st.selected_doc_ids:
            kb_snip = self._kb_snippets_from_ids(st.selected_doc_ids)

        try:
            reply = self.openai.chat(chat_id, update.message.text, kb_snip)
        except Exception as e:
            logger.error("Chat failed: %s", e, exc_info=True)
            reply = f"Ошибка обращения к OpenAI: {e}"

        await update.message.reply_text(reply)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        st = self._get_state(chat_id)

        file_id = update.message.voice.file_id
        f = await context.bot.get_file(file_id)
        bx = httpx.get(f.file_path, timeout=60.0)
        bx.raise_for_status()

        await self._typing(update, context, 0.5)
        try:
            text = self.openai.transcribe(bx.content, filename_hint="audio.ogg")
            await update.message.reply_text(f"🎙️ Вы сказали: {text}")
        except Exception as e:
            await update.message.reply_text(f"Ошибка распознавания: {e}")
            return

        # затем — текстовый ответ
        kb_snip = None
        if st.kb_enabled and st.selected_doc_ids:
            kb_snip = self._kb_snippets_from_ids(st.selected_doc_ids)

        try:
            reply = self.openai.chat(chat_id, text, kb_snip)
        except Exception as e:
            reply = f"Ошибка обращения к OpenAI: {e}"

        await update.message.reply_text(reply)

    async def on_file_or_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        При получении файлов — **не** добавляем сразу в БЗ.
        Короткий анализ и предложение использовать /kb для добавления.
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
                "Добавление в БЗ недоступно для изображений. Используйте /kb для работы с документами."
            )
            return
