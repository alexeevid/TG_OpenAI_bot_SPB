from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from datetime import datetime
from typing import List, Optional, Set

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    BotCommand,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.db.session import SessionLocal
from bot.openai_helper import OpenAIHelper
from bot.db.models import Conversation

logger = logging.getLogger(__name__)


def style_system_hint(style: str) -> (str, float):
    style = (style or "pro").lower()
    if style == "pro":
        return (
            "Отвечай кратко, по делу и профессионально. Используй строгую терминологию, избегай воды. "
            "Если данных недостаточно — явно укажи, что нужно уточнить.",
            0.2,
        )
    if style == "expert":
        return (
            "Дай развёрнутый экспертный ответ: глубина, сопоставления, альтернативы, тон — наставника. "
            "Добавляй структурирование: списки, шаги, caveats.",
            0.35,
        )
    if style == "user":
        return (
            "Объясняй простым языком, как для непрофессионала. Короткие фразы, примеры из быта.",
            0.5,
        )
    if style == "ceo":
        return (
            "Отвечай как собственник бизнеса уровня EMBA/DBA: стратегия, риски, бюджет, эффект, KPI. "
            "Сфокусируйся на принятии решений и следующем шаге.",
            0.25,
        )
    return ("", 0.3)


def only_allowed(func):
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if self.allowed and uid not in self.allowed and uid not in self.admins:
            await update.effective_message.reply_text("⛔ Доступ запрещён.")
            return
        return await func(self, update, context)
    return wrapper


class TypingIndicator:
    def __init__(self, bot, chat_id, interval: float = 4.0):
        self.bot = bot
        self.chat_id = chat_id
        self.interval = interval
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def __aenter__(self):
        async def _loop():
            try:
                while not self._stop.is_set():
                    await self.bot.send_chat_action(chat_id=self.chat_id, action=ChatAction.TYPING)
                    await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                pass

        self._task = asyncio.create_task(_loop())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._task:
            with contextlib.suppress(asyncio.CancelledError):
                self._task.cancel()


async def _set_global_commands(app: Application):
    cmds = [
        BotCommand("help", "Помощь"),
        BotCommand("reset", "Сброс контекста"),
        BotCommand("stats", "Статистика"),
        BotCommand("kb", "База знаний (включить/исключить документы)"),
        BotCommand("model", "Выбор модели OpenAI"),
        BotCommand("dialogs", "Список диалогов (/dialog <id> — перейти)"),
        BotCommand("image", "Генерация изображения"),
        BotCommand("web", "Вопрос с веб‑поиском"),
        BotCommand("style", "Режим ответа: Профессиональный/Экспертный/Пользовательский/СЕО"),
    ]
    await app.bot.set_my_commands(cmds)
    with contextlib.suppress(Exception):
        from telegram import (
            BotCommandScopeAllPrivateChats,
            BotCommandScopeAllGroupChats,
            BotCommandScopeAllChatAdministrators,
        )
        for scope in (
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
        ):
            await app.bot.set_my_commands([], scope=scope)


class ChatGPTTelegramBot:
    def __init__(self, openai: OpenAIHelper, settings):
        self.openai = openai
        self.allowed: Set[int] = set(
            getattr(settings, "allowed_set", []) or getattr(settings, "allowed_user_ids", [])
        )
        self.admins: Set[int] = set(
            getattr(settings, "admin_set", []) or getattr(settings, "admin_user_ids", [])
        )

        self.default_model: str = getattr(settings, "openai_model", None) or openai.model
        self.image_model: Optional[str] = getattr(settings, "image_model", None) or openai.image_model
        self.enable_image_generation: bool = bool(getattr(settings, "enable_image_generation", True))

        self.allowed_models_whitelist: List[str] = getattr(settings, "allowed_models_whitelist", []) or []
        self.denylist_models: List[str] = getattr(settings, "denylist_models", []) or []

    def _get_db(self) -> Session:
        return SessionLocal()

    def install(self, app: Application):
        app.add_handler(CommandHandler("start", self.on_start))
        app.add_handler(CommandHandler("help", self.on_help))
        app.add_handler(CommandHandler("reset", self.on_reset))
        app.add_handler(CommandHandler("stats", self.on_stats))
        app.add_handler(CommandHandler("kb", self.on_kb))
        app.add_handler(CommandHandler("model", self.on_model))
        app.add_handler(CommandHandler("web", self.on_web))
        app.add_handler(CommandHandler("image", self.on_image))
        app.add_handler(CommandHandler("dialogs", self.on_dialogs))
        app.add_handler(CommandHandler("style", self.on_style))

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        
        # PTB v20.x: post_init — это атрибут-колбэк, а не вызываемый метод
        if getattr(app, "post_init", None) is None:
            app.post_init = _set_global_commands
        else:
            # если уже кто-то задал колбэк, оборачиваем, чтобы не потерять чужой
            prev_cb = app.post_init
            async def _chained_post_init(application):
                if prev_cb:
                    await prev_cb(application)
                await _set_global_commands(application)
            app.post_init = _chained_post_init

    @only_allowed
    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет! Я готов к работе.\n"
            "Команды: /help, /reset, /stats, /kb, /model, /dialogs, /image, /web, /style"
        )

    @only_allowed
    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "/reset — сброс контекста\n"
            "/stats — статистика\n"
            "/kb — база знаний (включить/исключить документы)\n"
            "/model — выбор модели OpenAI\n"
            "/dialogs — список диалогов, /dialog <id> — перейти\n"
            "/image — генерация изображения\n"
            "/web — вопрос с веб‑поиском\n"
            "/style — режим ответа"
        )

    @only_allowed
    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        await update.message.reply_text("🔄 Новый диалог создан. Контекст очищен.")

    @only_allowed
    async def on_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        dialog_title = context.user_data.get("dialog_title") or context.user_data.get("title") or "Диалог"
        model = context.user_data.get("model", self.default_model)
        style = context.user_data.get("style", "pro")
        kb_enabled = bool(context.user_data.get("kb_enabled", False))
        selected_docs = context.user_data.get("kb_selected_docs") or context.user_data.get("kb_selected") or []
        if isinstance(selected_docs, set):
            selected_docs = list(selected_docs)
        if selected_docs and not isinstance(selected_docs[0], str):
            selected_docs = [str(d) for d in selected_docs]
        docs_line = ", ".join(selected_docs[:10]) + ("…" if len(selected_docs) > 10 else "")
        docs_cnt = len(selected_docs)

        msg = (
            "📊 Статистика:\n"
            f"- Диалог: {dialog_title}\n"
            f"- Модель: {model}\n"
            f"- Стиль: {style.capitalize()}\n"
            f"- База знаний: {'включена' if kb_enabled else 'выключена'}\n"
            f"- Документов выбрано: {docs_cnt}\n"
        )
        if docs_cnt:
            msg += f"- В контексте: {docs_line}\n"
        await update.message.reply_text(msg)

    @only_allowed
    async def on_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Меню Базы знаний доступно в текущей сборке через ваши кнопки выбора документов.")

    @only_allowed
    async def on_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cur = context.user_data.get("model", self.default_model)
        models = self.openai.list_models(
            whitelist=self.allowed_models_whitelist or None,
            denylist=self.denylist_models or None,
        )
        if not models:
            await update.message.reply_text("Не удалось получить список моделей.")
            return
        rows = []
        row = []
        for m in models:
            title = f"✅ {m}" if m == cur else m
            row.append(InlineKeyboardButton(title, callback_data=f"set_model:{m}"))
            if len(row) == 3:
                rows.append(row); row = []
        if row:
            rows.append(row)
        await update.message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(rows))

    @only_allowed
    async def on_web(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = (update.message.text or "").split(maxsplit=1)
        if len(q) < 2:
            await update.message.reply_text("Использование: /web <запрос>")
            return

        query = q[1].strip()
        model = context.user_data.get("model", self.default_model)

        async with TypingIndicator(context.bot, update.effective_chat.id):
            text, cites = await asyncio.to_thread(self.openai.answer_with_web, query, model=model)

        if cites:
            refs = "\n".join([f"• {c['title']}: {c['url']}" for c in cites])
            reply = f"{text}\n\n<b>Источники</b>:\n{refs}"
            await update.message.reply_text(reply, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            await update.message.reply_text(
                text + "\n\n⚠️ Модель не вернула явных ссылок-источников для этого ответа.",
                disable_web_page_preview=True
            )

    @only_allowed
    async def on_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = (update.message.text or "").split(maxsplit=1)
        if len(args) < 2:
            await update.message.reply_text("Использование: /image <описание изображения>")
            return
        prompt = args[1].strip()
        model = self.image_model
        if not self.enable_image_generation:
            await update.message.reply_text("Генерация изображений отключена конфигурацией.")
            return

        tmp_png = None
        try:
            async with TypingIndicator(context.bot, update.effective_chat.id):
                img_bytes = await asyncio.to_thread(self.openai.generate_image, prompt, model=model)

            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tf:
                tf.write(img_bytes)
                tmp_png = tf.name

            await update.message.reply_photo(photo=open(tmp_png, "rb"), caption=f"🖼️ Итоговый промпт:\n{prompt}")
        except Exception as e:
            logger.exception("Image generation failed: %s", e)
            await update.message.reply_text(f"Ошибка генерации изображения: {e}")
        finally:
            if tmp_png:
                with contextlib.suppress(Exception):
                    os.unlink(tmp_png)

    @only_allowed
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_text = (update.message.text or "").strip()
        style = context.user_data.get("style", "pro")
        sys_hint, temp = style_system_hint(style)
        model = context.user_data.get("model", self.default_model)

        messages = [
            {"role": "system", "content": sys_hint},
            {"role": "user", "content": user_text},
        ]

        async with TypingIndicator(context.bot, update.effective_chat.id):
            answer = await asyncio.to_thread(
                self.openai.chat, messages, temperature=temp, max_output_tokens=4096, model=model
            )

        await update.message.reply_text(answer or "Пустой ответ.")

    @only_allowed
    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        voice = update.message.voice
        if not voice:
            return

        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tf:
            tmp_ogg = tf.name

        try:
            file = await context.bot.get_file(voice.file_id)
            await file.download_to_drive(custom_path=tmp_ogg)
        except Exception as e:
            await update.message.reply_text(f"Не удалось скачать голосовое: {e}")
            with contextlib.suppress(Exception):
                os.unlink(tmp_ogg)
            return

        try:
            async with TypingIndicator(context.bot, update.effective_chat.id):
                text = await asyncio.to_thread(self.openai.transcribe, tmp_ogg)

                style = context.user_data.get("style", "pro")
                sys_hint, temp = style_system_hint(style)
                model = context.user_data.get("model", self.default_model)

                messages = [
                    {"role": "system", "content": sys_hint},
                    {"role": "user", "content": (text or '').strip()},
                ]
                answer = await asyncio.to_thread(
                    self.openai.chat, messages, temperature=temp, max_output_tokens=4096, model=model
                )
        except Exception as e:
            await update.message.reply_text(f"Не удалось распознать/ответить: {e}")
            return
        finally:
            with contextlib.suppress(Exception):
                os.unlink(tmp_ogg)

        await update.message.reply_text(f"🗣️ Вы сказали:\n{text.strip() if text else ''}")
        await update.message.reply_text(answer or "Пустой ответ.")

    @only_allowed
    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        doc = update.message.document
        if not doc:
            return
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmp_path = tf.name
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(custom_path=tmp_path)
            size_mb = round((doc.file_size or 0) / (1024 * 1024), 2)
            info = f"📄 Файл: {doc.file_name} ({size_mb} МБ, {doc.mime_type})\n\n"
            info += "Файл проанализирован, но НЕ добавлен в Базу знаний.\n" \
                    "Чтобы добавить — используйте меню БЗ/команду, доступную админам."
            await update.message.reply_text(info)
        except Exception as e:
            await update.message.reply_text(f"Ошибка загрузки файла: {e}")
        finally:
            with contextlib.suppress(Exception):
                os.unlink(tmp_path)

    @only_allowed
    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        photo = (update.message.photo or [])[-1] if update.message.photo else None
        if not photo:
            return
        await update.message.reply_text(
            "🖼️ Фото получено. Пока я делаю только текстовые ответы и генерацию изображений по описанию. "
            "Если нужно — можно добавить анализ изображений/vision."
        )

    @only_allowed
    async def on_style(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        parts = (update.message.text or "").split(maxsplit=1)
        styles = {"pro": "Профессиональный", "expert": "Экспертный", "user": "Пользовательский", "ceo": "СЕО"}
        if len(parts) == 2:
            key = parts[1].strip().lower()
            if key in styles:
                context.user_data["style"] = key
                await update.message.reply_text(f"Стиль установлен: {styles[key]}")
                return
            else:
                await update.message.reply_text("Использование: /style pro|expert|user|ceo")
                return
        rows = [
            [InlineKeyboardButton("Профессиональный", callback_data="set_style:pro"),
             InlineKeyboardButton("Экспертный", callback_data="set_style:expert")],
            [InlineKeyboardButton("Пользовательский", callback_data="set_style:user"),
             InlineKeyboardButton("СЕО", callback_data="set_style:ceo")],
        ]
        await update.message.reply_text("Выберите стиль ответа:", reply_markup=InlineKeyboardMarkup(rows))

    def _render_dialogs_page(self, db: Session, uid: int, page: int = 0, page_size: int = 10):
        total = db.execute(select(Conversation).where(Conversation.user_id == uid)).scalars().all()
        total_count = len(total)
        rows = sorted(
            total,
            key=lambda c: (getattr(c, "updated_at", None) or getattr(c, "created_at", None) or datetime.min),
            reverse=True,
        )
        start = page * page_size
        end = start + page_size
        slice_rows = rows[start:end]
        if not slice_rows:
            return "У вас пока нет диалогов.", None

        kb = []
        for c in slice_rows:
            title = (getattr(c, "title", None) or f"Диалог #{c.id}").strip()
            if len(title) > 40:
                title = title[:37] + "…"
            kb.append([
                InlineKeyboardButton(f"#{c.id}: {title}", callback_data=f"open_dlg:{c.id}"),
                InlineKeyboardButton("🗑 Удалить", callback_data=f"del_dlg:{c.id}"),
            ])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"dlg_page:{page-1}"))
        if end < total_count:
            nav.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"dlg_page:{page+1}"))
        if nav:
            kb.append(nav)

        text = "Выберите диалог:\n— Нажмите на строку, чтобы перейти, или 🗑 — удалить."
        return text, InlineKeyboardMarkup(kb)

    @only_allowed
    async def on_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        db = self._get_db()
        try:
            text, markup = self._render_dialogs_page(db, uid, page=0)
            await update.message.reply_text(text, reply_markup=markup)
        except Exception as e:
            await update.message.reply_text(f"Ошибка получения списка диалогов: {e}")
        finally:
            db.close()

    @only_allowed
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()

        data = q.data or ""

        if data.startswith("set_model:"):
            model = data.split(":", 1)[1]
            context.user_data["model"] = model
            await q.edit_message_text(f"Модель установлена: {model}")
            return

        if data.startswith("set_style:"):
            st = data.split(":", 1)[1]
            context.user_data["style"] = st
            human = {"pro":"Профессиональный","expert":"Экспертный","user":"Пользовательский","ceo":"СЕО"}.get(st, st)
            await q.edit_message_text(f"Стиль установлен: {human}")
            return

        if data.startswith("dlg_page:"):
            try:
                page = int(data.split(":", 1)[1])
            except Exception:
                page = 0
            uid = update.effective_user.id if update.effective_user else None
            db = self._get_db()
            try:
                text, markup = self._render_dialogs_page(db, uid, page=page)
                await q.edit_message_text(text, reply_markup=markup)
            except Exception as e:
                await q.edit_message_text(f"Ошибка пагинации: {e}")
            finally:
                db.close()
            return

        if data.startswith("open_dlg:"):
            dlg_id = int(data.split(":", 1)[1])
            uid = update.effective_user.id if update.effective_user else None
            db = self._get_db()
            try:
                row = db.execute(
                    select(Conversation).where(Conversation.id == dlg_id, Conversation.user_id == uid)
                ).scalar_one_or_none()
                if not row:
                    await q.edit_message_text("Диалог не найден или не принадлежит вам.")
                    return

                context.user_data["dialog_id"] = row.id
                context.user_data["dialog_title"] = row.title or f"Диалог #{row.id}"

                await q.edit_message_text(f"✅ Перешли в диалог #{row.id}: {context.user_data['dialog_title']}")
            except Exception as e:
                await q.edit_message_text(f"Ошибка открытия диалога: {e}")
            finally:
                db.close()
            return

        if data.startswith("del_dlg:"):
            dlg_id = int(data.split(":", 1)[1])
            uid = update.effective_user.id if update.effective_user else None
            db = self._get_db()
            try:
                row = db.execute(
                    select(Conversation).where(Conversation.id == dlg_id, Conversation.user_id == uid)
                ).scalar_one_or_none()
                if not row:
                    await q.edit_message_text("Диалог не найден или не принадлежит вам.")
                    return

                from sqlalchemy import delete as sqldelete
                db.execute(sqldelete(Conversation).where(Conversation.id == dlg_id))
                db.commit()

                if context.user_data.get("dialog_id") == dlg_id:
                    context.user_data.pop("dialog_id", None)
                    context.user_data.pop("dialog_title", None)

                text, markup = self._render_dialogs_page(db, uid, page=0)
                await q.edit_message_text("🗑 Диалог удалён.\n\n" + text, reply_markup=markup)
            except Exception as e:
                db.rollback()
                await q.edit_message_text(f"Ошибка удаления: {e}")
            finally:
                db.close()
            return

    def _get_db(self) -> Session:
        return SessionLocal()
