# bot/telegram_bot.py
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from contextlib import suppress
from datetime import datetime
from typing import List, Optional, Set

from sqlalchemy import text
from sqlalchemy.orm import Session

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
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
from bot.db.models import Document  # предполагается, что у вас есть эта модель
from bot.openai_helper import OpenAIHelper

logger = logging.getLogger(__name__)


# ---------------- Utilities ----------------

def style_system_hint(style: str) -> (str, float):
    """
    Возвращает (system_prompt, temperature) по выбранному стилю.
    """
    style = (style or "pro").lower()
    if style == "pro":  # Профессиональный
        return (
            "Отвечай кратко, по делу и профессионально. "
            "Используй строгую терминологию, избегай воды. "
            "Если данных недостаточно — явно укажи, что нужно уточнить.",
            0.2,
        )
    if style == "expert":  # Экспертный
        return (
            "Дай развёрнутый экспертный ответ: глубина, сопоставления, альтернативы, тон — наставника. "
            "Добавляй структурирование: списки, шаги, caveats.",
            0.35,
        )
    if style == "user":  # Пользовательский
        return (
            "Объясняй простым языком, как для непрофессионала. "
            "Короткие фразы, примеры из быта.",
            0.5,
        )
    if style == "ceo":  # СЕО
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
    """
    Периодически отправляет ChatAction.TYPING, пока выполняется долгий вызов.
    """

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
            with suppress(asyncio.CancelledError):
                self._task.cancel()


# ---------------- Bot ----------------

class ChatGPTTelegramBot:
    def __init__(self, openai: OpenAIHelper, settings):
        self.openai = openai
        # списки пользователей из настроек (поддержим старые/новые поля)
        self.allowed: Set[int] = set(
            getattr(settings, "allowed_user_ids", None)
            or getattr(settings, "allowed_set", None)
            or []
        )
        self.admins: Set[int] = set(
            getattr(settings, "admin_user_ids", None)
            or getattr(settings, "admin_set", None)
            or []
        )

        # конфиг по умолчанию
        self.default_model: str = getattr(settings, "openai_model", None) or openai.model
        self.image_model: Optional[str] = getattr(settings, "image_model", None) or openai.image_model
        self.enable_image_generation: bool = bool(getattr(settings, "enable_image_generation", True))

        # Модели и ограничения из ENV (опционально)
        self.allowed_models_whitelist: List[str] = getattr(settings, "allowed_models_whitelist", []) or []
        self.denylist_models: List[str] = getattr(settings, "denylist_models", []) or []

    # ---- Helpers ----

    def _get_db(self) -> Session:
        return SessionLocal()

    def _get_active_conv(self, chat_id: int, db: Session):
        # В проекте уже есть таблица conversations — используем вашу логику, здесь — заглушка
        return None

    def _ensure_conv_title(self, conv, user_text: str, db: Session):
        # Ваша логика именования диалогов; оставим как есть, если реализовано в другом месте
        pass

    # ---- Install ----

    def install(self, app: Application):
        app.add_handler(CommandHandler("start", self.on_start))
        app.add_handler(CommandHandler("help", self.on_help))
        app.add_handler(CommandHandler("reset", self.on_reset))
        app.add_handler(CommandHandler("stats", self.on_stats))
        app.add_handler(CommandHandler("model", self.on_model))
        app.add_handler(CommandHandler("web", self.on_web))
        app.add_handler(CommandHandler("image", self.on_image))

        # диагностика pgvector (админ)
        app.add_handler(CommandHandler("debug_pgvector", self.on_debug_pgvector))

        # обычный текст
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        # голосовые
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        # документы/фото — анализ, без автозагрузки в БЗ
        app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))

        # колбэки меню (например, выбор модели)
        app.add_handler(CallbackQueryHandler(self.on_callback))

    # ---- Commands ----

    @only_allowed
    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет! Я готов к работе.\n"
            "Команды: /help, /reset, /stats, /model, /image, /web"
        )

    @only_allowed
    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "/reset — сброс контекста\n"
            "/stats — статистика\n"
            "/model — выбор модели OpenAI\n"
            "/image — генерация изображения\n"
            "/web <запрос> — поиск в интернете с источниками"
        )

    @only_allowed
    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        await update.message.reply_text("🔄 Новый диалог создан. Контекст очищен.")

    @only_allowed
    async def on_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        style = context.user_data.get("style", "pro")
        model = context.user_data.get("model", self.default_model)
        await update.message.reply_text(
            "📊 Статистика:\n"
            f"- Модель: {model}\n"
            f"- Стиль: {style}\n"
        )

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
        # соберём клавиатуру по 3 в ряд
        rows = []
        row = []
        for m in models:
            title = f"✅ {m}" if m == cur else m
            row.append(InlineKeyboardButton(title, callback_data=f"set_model:{m}"))
            if len(row) == 3:
                rows.append(row)
                row = []
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
            await update.message.reply_text(text)

    @only_allowed
    async def on_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # /image описание
        args = (update.message.text or "").split(maxsplit=1)
        if len(args) < 2:
            await update.message.reply_text("Использование: /image <описание изображения>")
            return
        prompt = args[1].strip()
        model = self.image_model

        if not self.enable_image_generation:
            await update.message.reply_text("Генерация изображений отключена конфигурацией.")
            return

        try:
            async with TypingIndicator(context.bot, update.effective_chat.id):
                img_bytes = await asyncio.to_thread(self.openai.generate_image, prompt, model=model)

            # Отправляем картинку
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tf:
                tf.write(img_bytes)
                tmp_png = tf.name

            await update.message.reply_photo(
                photo=open(tmp_png, "rb"),
                caption=f"🖼️ Итоговый промпт:\n{prompt}",
            )
        except Exception as e:
            logger.exception("Image generation failed: %s", e)
            await update.message.reply_text(f"Ошибка генерации изображения: {e}")
        finally:
            with suppress(Exception):
                os.unlink(tmp_png)  # noqa

    # ---- Messages ----

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
                self.openai.chat,
                messages,
                temperature=temp,
                max_output_tokens=4096,
                model=model,
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
            with suppress(Exception):
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
                    {"role": "user", "content": (text or "").strip()},
                ]
                answer = await asyncio.to_thread(
                    self.openai.chat,
                    messages,
                    temperature=temp,
                    max_output_tokens=4096,
                    model=model,
                )
        except Exception as e:
            await update.message.reply_text(f"Не удалось распознать/ответить: {e}")
            return
        finally:
            with suppress(Exception):
                os.unlink(tmp_ogg)

        await update.message.reply_text(f"🗣️ Вы сказали:\n{text.strip() if text else ''}")
        await update.message.reply_text(answer or "Пустой ответ.")

    # ---- Files (анализ без автозагрузки в БЗ) ----

    @only_allowed
    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        doc = update.message.document
        if not doc:
            return

        # Скачиваем во временный файл
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
            with suppress(Exception):
                os.unlink(tmp_path)

    @only_allowed
    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Берём фото максимального размера
        photo = (update.message.photo or [])[-1] if update.message.photo else None
        if not photo:
            return
        await update.message.reply_text(
            "🖼️ Фото получено. "
            "Пока я делаю только текстовые ответы и генерацию изображений по описанию. "
            "Если нужно — можно добавить анализ изображений/vision."
        )

    # ---- Callback buttons ----

    @only_allowed
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()

        data = q.data or ""
        if data.startswith("set_model:"):
            model = data.split(":", 1)[1]
            context.user_data["model"] = model
            await q.edit_message_text(f"Модель установлена: {model}")

    # ---- Admin / Diagnostics ----

    @only_allowed
    async def on_debug_pgvector(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /debug_pgvector — проверка доступности/установки расширения pgvector.
        Доступно только администратору.
        """
        if self.admins and (not update.effective_user or update.effective_user.id not in self.admins):
            await update.message.reply_text("⛔ Доступно только администратору.")
            return

        try:
            eng = SessionLocal.bind  # SQLAlchemy Engine
            lines = []
            with eng.connect() as conn:
                ver = conn.execute(text("SELECT version();")).scalar()
                lines.append(f"Postgres: {ver}")

                avail = conn.execute(text("""
                    SELECT name, default_version, installed_version
                    FROM pg_available_extensions
                    WHERE name='vector';
                """)).fetchall()
                if avail:
                    n, dv, iv = avail[0]
                    lines.append(f"pg_available_extensions: {n} (default={dv}, installed={iv})")
                else:
                    lines.append("pg_available_extensions: vector НЕ найден")

                created = False
                err = None
                try:
                    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                    conn.commit()
                    created = True
                except Exception as e:
                    err = str(e)

                if created:
                    lines.append("CREATE EXTENSION vector: OK (или уже было)")
                else:
                    lines.append(f"CREATE EXTENSION vector: ошибка: {err}")

                ext = conn.execute(text("""
                    SELECT extname, extversion FROM pg_extension WHERE extname='vector';
                """)).fetchall()
                if ext:
                    en, ev = ext[0]
                    lines.append(f"pg_extension: установлено {en} v{ev}")
                else:
                    lines.append("pg_extension: vector НЕ установлен")

            await update.message.reply_text("🔎 Проверка pgvector:\n" + "\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"Ошибка проверки: {e}")
