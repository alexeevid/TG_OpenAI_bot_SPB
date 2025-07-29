import asyncio
import logging
import os
import tempfile
from contextlib import suppress
from functools import wraps
from typing import Optional, List, Tuple, Dict
from io import BytesIO
from datetime import datetime

import yadisk
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    InputFile,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators,
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

from sqlalchemy.orm import Session
from bot.db.session import SessionLocal
from bot.db.models import Document, Conversation
from bot.openai_helper import OpenAIHelper
from bot.settings import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Индикатор «набирает… / загружает фото… / записывает голос…»
# ---------------------------------------------------------------------------
class ChatActionSender:
    def __init__(self, *, action: ChatAction, chat_id: int, bot, interval: float = 4.0):
        self.action = action
        self.chat_id = chat_id
        self.bot = bot
        self.interval = interval
        self._task: Optional[asyncio.Task] = None

    async def __aenter__(self):
        async def _runner():
            try:
                while True:
                    await self.bot.send_chat_action(chat_id=self.chat_id, action=self.action)
                    await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                pass

        self._task = asyncio.create_task(_runner())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._task:
            self._task.cancel()
            with suppress(Exception):
                await self._task


# ---------- Access decorator ----------
def only_allowed(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        # Если allowed пустой — доступ всем
        if self.allowed and uid not in self.allowed:
            await update.effective_message.reply_text("⛔ Доступ ограничен.")
            return
        return await func(self, update, context)
    return wrapper


# ---------- Styles ----------
STYLE_LABELS = {
    "pro": "Профессиональный",
    "expert": "Экспертный",
    "user": "Пользовательский",
    "ceo": "СЕО",
}

def style_system_hint(style: str):
    s = (style or "pro").lower()
    if s == "pro":
        return ("Отвечай как высокопрофессиональный консультант. Максимально точно, лаконично, по делу, без воды.", 0.2)
    if s == "expert":
        return ("Отвечай как эксперт-практик с глубокими знаниями темы. Приводи точные формулировки и причинно-следственные связи.", 0.3)
    if s == "user":
        return ("Объясняй просто, как обычный опытный пользователь. Можешь давать примеры и чуть более разговорный стиль.", 0.6)
    if s == "ceo":
        return ("Отвечай как собственник бизнеса (EMBA/DBA): стратегия, ROI, риски, ресурсы, влияние на оргдизайн и культуру.", 0.25)
    return ("Отвечай профессионально и по делу.", 0.3)


class ChatGPTTelegramBot:
    def __init__(self, openai: OpenAIHelper, settings: Settings):
        self.openai = openai
        self.settings = settings
        self.allowed = set(settings.allowed_set) if settings.allowed_set else set()
        self.admins = set(settings.admin_set) if settings.admin_set else set()

    # ---------- Wiring ----------
    def install(self, app: Application):
        # Команды
        app.add_handler(CommandHandler("start", self.on_start))
        app.add_handler(CommandHandler("help", self.on_help))
        app.add_handler(CommandHandler("reset", self.on_reset))
        app.add_handler(CommandHandler("stats", self.on_stats))
        app.add_handler(CommandHandler("kb", self.on_kb))
        app.add_handler(CommandHandler("model", self.on_model))
        app.add_handler(CommandHandler("dialogs", self.on_dialogs))
        app.add_handler(CommandHandler("dialog", self.on_dialog_select))
        app.add_handler(CommandHandler("mode", self.on_mode))
        app.add_handler(CommandHandler("img", self.on_img))
        app.add_handler(CommandHandler("cancelpass", self.on_cancel_pass))
        app.add_handler(CommandHandler("del", self.on_delete_dialogs))
        app.add_handler(CommandHandler("reload_menu", self.on_reload_menu))
        app.add_handler(CommandHandler("cancelupload", self.on_cancel_upload))
        app.add_handler(CommandHandler("web", self.cmd_web))  # новый web-поиск

        # Callback-и
        app.add_handler(CallbackQueryHandler(self.on_callback))

        # Сообщения
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))  # голосовые
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file_message))  # фото/документы
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))  # обычный текст

        # Меню команд для всех скоупов
        app.post_init = self._post_init_commands

    async def _post_init_commands(self, app: Application):
        cmds = [
            BotCommand("start", "Запуск и меню"),
            BotCommand("help", "Помощь"),
            BotCommand("reset", "Сброс контекста"),
            BotCommand("stats", "Статистика"),
            BotCommand("kb", "База знаний"),
            BotCommand("model", "Выбор модели"),
            BotCommand("dialogs", "Список диалогов"),
            BotCommand("img", "Сгенерировать изображение"),
            BotCommand("mode", "Стиль ответов"),
            BotCommand("web", "Поиск в интернете"),
            BotCommand("del", "Удалить диалоги"),
            BotCommand("reload_menu", "Обновить меню у всех"),
            BotCommand("cancelupload", "Выйти из режима загрузки в БЗ"),
        ]
        await self._set_all_scopes_commands(app, cmds)

    async def _set_all_scopes_commands(self, app: Application, cmds: List[BotCommand]):
        scopes = [
            None,
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
        ]
        langs = [None, "ru", "en"]

        for sc in scopes:
            for lang in langs:
                with suppress(Exception):
                    await app.bot.delete_my_commands(scope=sc, language_code=lang)

        for sc in scopes:
            for lang in langs:
                with suppress(Exception):
                    await app.bot.set_my_commands(commands=cmds, scope=sc, language_code=lang)

    # ---------- DB helpers ----------
    def _get_db(self) -> Session:
        return SessionLocal()

    def _get_active_conv(self, chat_id: int, db: Session) -> Conversation:
        conv = (
            db.query(Conversation)
            .filter_by(chat_id=chat_id, is_active=True)
            .order_by(Conversation.id.desc())
            .first()
        )
        if not conv:
            conv = Conversation(chat_id=chat_id, title="Диалог")
            db.add(conv)
            db.commit()
            db.refresh(conv)
        return conv

    # ---------- Title helpers ----------
    @staticmethod
    def _short_title_from_text(text: str, limit: int = 48) -> str:
        base = (text or "").strip().splitlines()[0]
        base = " ".join(base.split())
        return (base[:limit] + "…") if len(base) > limit else base

    def _ensure_conv_title(self, conv: Conversation, first_user_text: str, db: Session):
        base = conv.title or "Диалог"
        created = conv.created_at.strftime("%Y-%m-%d")
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        if base == "Диалог":
            short = self._short_title_from_text(first_user_text) or "Диалог"
            conv.title = f"{short} · {created} · upd {now}"
        else:
            parts = base.split(" · ")
            if len(parts) >= 2:
                conv.title = " ".join(parts[:2]) + f" · upd {now}"
            else:
                conv.title = f"{base} · upd {now}"
        db.add(conv)
        db.commit()

    # ---------- Commands ----------
    @only_allowed
    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет! Я готов к работе.\n"
            "Команды: /help, /reset, /stats, /kb, /model, /dialogs, /img, /mode, /web, /del, /reload_menu"
        )

    @only_allowed
    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "/reset — сброс контекста\n"
            "/stats — статистика\n"
            "/kb — база знаний (включить/исключить документы, пароли, загрузка админом)\n"
            "/model — выбор модели OpenAI (персонально для вашего чата)\n"
            "/mode — стиль ответов (Профессиональный/Экспертный/Пользовательский/СЕО)\n"
            "/dialogs — список диалогов, /dialog <id> — вернуться\n"
            "/img <описание> — сгенерировать изображение\n"
            "/web <запрос> — поиск в интернете\n"
            "/del — удалить диалоги\n"
            "/reload_menu — обновить меню у всех\n"
            "/cancelupload — выйти из режима загрузки в БЗ"
        )

    @only_allowed
    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        db.query(Conversation).filter_by(chat_id=chat_id, is_active=True).update({"is_active": False})
        db.commit()
        newc = Conversation(chat_id=chat_id, title="Диалог")
        db.add(newc)
        db.commit()
        await update.message.reply_text("🔄 Новый диалог создан. Контекст очищен.")
        # Сбрасываем только временные состояния; выбранные документы и персональную модель НЕ трогаем
        context.user_data.pop("await_password_for", None)

    @only_allowed
    async def on_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        conv = self._get_active_conv(chat_id, db)
        docs = context.user_data.get("kb_selected_ids", set()) or set()
        kb_enabled = context.user_data.get("kb_enabled", True)
        style = context.user_data.get("style", "pro")
        style_label = STYLE_LABELS.get(style, "Профессиональный")
        user_model = context.user_data.get("model", self.openai.model)

        title = conv.title or "Диалог"
        names: List[str] = []
        if docs:
            q = db.query(Document).filter(Document.id.in_(list(docs))).all()
            names = [d.title for d in q]

        text = (
            f"📊 Статистика:\n"
            f"- Диалог: {title}\n"
            f"- Модель: {user_model}\n"
            f"- Стиль: {style_label}\n"
            f"- База знаний: {'включена' if kb_enabled else 'выключена'}\n"
            f"- Документов выбрано: {len(docs)}"
        )
        if names:
            text += "\n- В контексте: " + ", ".join(names[:10])
            if len(names) > 10:
                text += f" и ещё {len(names) - 10}…"

        await update.message.reply_text(text)

    # ---------- Knowledge Base ----------
    @only_allowed
    async def on_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        self._get_active_conv(chat_id, db)  # ensure exists

        kb_enabled = context.user_data.get("kb_enabled", True)
        selected = context.user_data.get("kb_selected_ids", set())
        docs = db.query(Document).order_by(Document.id.asc()).limit(50).all()

        rows = []
        for d in docs:
            mark = "✅" if d.id in selected else "➕"
            rows.append([InlineKeyboardButton(f"{mark} {d.title}", callback_data=f"kb_toggle:{d.id}")])

        rows.append([InlineKeyboardButton("🔄 Синхронизировать с Я.Диском", callback_data="kb_sync")])
        rows.append([InlineKeyboardButton(("🔕 Отключить БЗ" if kb_enabled else "🔔 Включить БЗ"), callback_data="kb_toggle_enabled")])
        rows.append([InlineKeyboardButton("🔐 Указать пароли для выбранных", callback_data="kb_pass_menu")])

        # Кнопка загрузки только для администраторов (или если список админов пуст — всем)
        is_admin = (not self.admins) or (update.effective_user and update.effective_user.id in self.admins)
        if is_admin:
            rows.append([InlineKeyboardButton("📥 Добавить из чата", callback_data="kb_upload_mode")])

        await update.message.reply_text(
            f"База знаний: {'включена' if kb_enabled else 'выключена'}.\n"
            "• Нажмите на документ, чтобы включить/исключить его из контекста.\n"
            "• «🔄 Синхронизировать» — подтянуть изменения с Я.Диска.\n"
            "• «📥 Добавить из чата» — загрузить новые файлы на Диск (только админ).",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    # ---------- Models ----------
    @only_allowed
    async def on_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        models_all = self.openai.list_models()
        current = context.user_data.get("model", self.openai.model)

        allow_list = getattr(self.settings, "allowed_models_whitelist", [])
        deny_list = getattr(self.settings, "denylist_models", [])
        allow = set(m.lower() for m in allow_list) if allow_list else None
        deny = set(m.lower() for m in deny_list)

        def _allowed(m: str) -> bool:
            ml = m.lower()
            if allow is not None and ml not in allow:
                return False
            if ml in deny:
                return False
            return True

        models = [m for m in models_all if _allowed(m)]
        prefer_keywords = ["gpt-4o", "gpt-4.1", "gpt-4", "gpt-3.5", "o4", "o3"]
        prefer = [m for m in models if any(k in m for k in prefer_keywords)]

        combined = []
        seen = set()
        for m in prefer + models:
            if m not in seen:
                seen.add(m)
                combined.append(m)
            if len(combined) >= 30:
                break

        items = combined or models[:30]
        if not items:
            await update.message.reply_text("Список моделей пуст — проверьте фильтры (whitelist/denylist).")
            return

        if current in items:
            items = [current] + [m for m in items if m != current]

        rows = []
        for m in items:
            label = f"✅ {m}" if m == current else m
            cb = "noop" if m == current else f"set_model:{m}"
            rows.append([InlineKeyboardButton(label, callback_data=cb)])

        await update.message.reply_text("Выберите модель (сохраняется только для этого чата):", reply_markup=InlineKeyboardMarkup(rows))

    # ---------- Modes ----------
    @only_allowed
    async def on_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        rows = [
            [InlineKeyboardButton("Профессиональный", callback_data="set_mode:pro")],
            [InlineKeyboardButton("Экспертный", callback_data="set_mode:expert")],
            [InlineKeyboardButton("Пользовательский", callback_data="set_mode:user")],
            [InlineKeyboardButton("СЕО", callback_data="set_mode:ceo")],
        ]
        await update.message.reply_text("Выберите стиль ответов:", reply_markup=InlineKeyboardMarkup(rows))

    # ---------- Images ----------
    @only_allowed
    async def on_img(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not getattr(self.settings, "enable_image_generation", True):
            await update.message.reply_text("Генерация изображений выключена администратором.")
            return

        prompt = " ".join(context.args) if context.args else ""
        if not prompt and update.message and update.message.reply_to_message:
            prompt = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        prompt = (prompt or "").strip()
        if not prompt:
            await update.message.reply_text(
                "Уточните описание, например: `/img логотип в стиле минимализм`",
                parse_mode="Markdown",
            )
            return

        try:
            async with ChatActionSender(
                action=ChatAction.UPLOAD_PHOTO,
                chat_id=update.effective_chat.id,
                bot=context.bot,
            ):
                png, used_prompt, used_model = await asyncio.to_thread(
                    self.openai.generate_image, prompt, size="1024x1024"
                )
            bio = BytesIO(png)
            bio.name = "image.png"
            bio.seek(0)
            caption = f"🖼️ Модель: {used_model}\n📝 Промпт: {used_prompt}"
            await update.message.reply_photo(photo=InputFile(bio, filename="image.png"), caption=caption)
        except Exception as e:
            await update.message.reply_text(f"Ошибка генерации изображения: {e}")

    # ---------- /web (Responses + web_search) ----------
    @only_allowed
    async def cmd_web(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /web <запрос> — поиск в интернете через OpenAI web_search tool. """
        q = (update.message.text or "").split(maxsplit=1)
        query = q[1].strip() if len(q) > 1 else ""
        if not query:
            await update.message.reply_text("Использование: /web <запрос>\nНапример: /web последние новости по ИИ")
            return

        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        except Exception:
            pass

        user_model = context.user_data.get("model", self.openai.model)
        text, cites = await asyncio.to_thread(self.openai.answer_with_web, query, model=user_model)

        if cites:
            bullets = []
            for i, c in enumerate(cites[:8], 1):
                title = c.get("title") or "Источник"
                url = c.get("url")
                bullets.append(f"{i}. {title}\n{url}")
            tail = "\n\nИсточники:\n" + "\n".join(bullets)
        else:
            tail = ""

        await update.message.reply_text(f"🔎 *Результаты по запросу:* {query}\n\n{text}{tail}", parse_mode="Markdown")

    # ---------- Dialogs ----------
    @only_allowed
    async def on_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        items = (
            db.query(Conversation)
            .filter_by(chat_id=chat_id)
            .order_by(Conversation.id.desc())
            .limit(10)
            .all()
        )
        if not items:
            await update.message.reply_text("Нет сохранённых диалогов.")
            return
        rows = [[InlineKeyboardButton(f"#{c.id} {c.title}", callback_data=f"goto_dialog:{c.id}")] for c in items]
        await update.message.reply_text("Выберите диалог:", reply_markup=InlineKeyboardMarkup(rows))

    @only_allowed
    async def on_delete_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        items = (
            db.query(Conversation)
            .filter_by(chat_id=chat_id)
            .order_by(Conversation.id.desc())
            .limit(15)
            .all()
        )
        if not items:
            await update.message.reply_text("Нет сохранённых диалогов.")
            return

        rows = [[InlineKeyboardButton(f"🗑️ #{c.id} {c.title}", callback_data=f"ask_del:{c.id}")] for c in items]
        rows.append([InlineKeyboardButton("🧹 Удалить все неактивные", callback_data="ask_del_all")])
        await update.message.reply_text("Выберите диалог для удаления:", reply_markup=InlineKeyboardMarkup(rows))

    @only_allowed
    async def on_dialog_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args or []
        if not args:
            await update.message.reply_text("Использование: /dialog <id>")
            return
        try:
            target = int(args[0])
        except ValueError:
            await update.message.reply_text("Некорректный id.")
            return

        db = self._get_db()
        chat_id = update.effective_chat.id
        db.query(Conversation).filter_by(chat_id=chat_id, is_active=True).update({"is_active": False})
        c = db.query(Conversation).filter_by(chat_id=chat_id, id=target).first()
        if not c:
            await update.message.reply_text("Диалог не найден.")
            return
        c.is_active = True
        db.commit()
        await update.message.reply_text(f"✅ Активирован диалог #{c.id} ({c.title}).")

    # ---------- Reload menu ----------
    @only_allowed
    async def on_reload_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.admins and (not update.effective_user or update.effective_user.id not in self.admins):
            await update.message.reply_text("⛔ Доступно только администратору.")
            return
        await self._post_init_commands(context.application)
        await update.message.reply_text(
            "✅ Меню обновлено для всех чатов и языков. Если изменения не видны, перезапустите чат или потяните список команд вниз."
        )

    # ---------- Callbacks ----------
    @only_allowed
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        data = q.data or ""

        # --- KB toggles ---
        if data.startswith("kb_toggle:"):
            doc_id = int(data.split(":")[1])
            selected = context.user_data.get("kb_selected_ids", set())
            adding = doc_id not in selected

            if adding:
                selected.add(doc_id)
            else:
                selected.remove(doc_id)

            context.user_data["kb_selected_ids"] = selected
            await q.edit_message_reply_markup(reply_markup=None)

            # Если документ только что ДОБАВЛЕН — проверим, нужно ли спросить пароль
            if adding:
                db = self._get_db()
                try:
                    doc = db.query(Document).filter_by(id=doc_id).first()
                    if doc:
                        kb_passwords: Dict[int, str] = context.user_data.get("kb_passwords", {}) or {}
                        if doc_id not in kb_passwords and await self._needs_password_for_doc(doc):
                            context.user_data["await_password_for"] = doc_id
                            await q.message.reply_text(
                                f"Документ «{doc.title}» защищён паролем.\n"
                                f"Пожалуйста, введите пароль одним сообщением.\n"
                                f"Команда для отмены: /cancelpass"
                            )
                            return
                finally:
                    with suppress(Exception):
                        db.close()

            await q.message.reply_text("Изменения применены. Нажмите /kb, чтобы обновить список.")

        elif data == "kb_toggle_enabled":
            cur = context.user_data.get("kb_enabled", True)
            context.user_data["kb_enabled"] = not cur
            await q.edit_message_text(
                f"База знаний: {'включена' if not cur else 'выключена'}. Нажмите /kb, чтобы обновить."
            )

        elif data == "kb_sync":
            is_admin = bool(self.admins) and (update.effective_user and update.effective_user.id in self.admins)
            if not self.admins or is_admin:
                await q.edit_message_text("Запускаю синхронизацию…")
                await self._kb_sync_internal(update, context)
            else:
                await q.edit_message_text("Доступно только администратору.")

        elif data == "kb_pass_menu":
            selected = context.user_data.get("kb_selected_ids", set())
            if not selected:
                await q.edit_message_text("Нет выбранных документов. Сначала выберите их в /kb.")
                return
            db = self._get_db()
            docs = db.query(Document).filter(Document.id.in_(list(selected))).all()
            rows = [[InlineKeyboardButton(f"🔐 {d.title}", callback_data=f"kb_pass:{d.id}")] for d in docs[:30]]
            rows.append([InlineKeyboardButton("❌ Отмена", callback_data="kb_pass_cancel")])
            await q.edit_message_text("Выберите документ для ввода пароля:", reply_markup=InlineKeyboardMarkup(rows))

        elif data.startswith("kb_pass:"):
            doc_id = int(data.split(":")[1])
            context.user_data["await_password_for"] = doc_id
            await q.edit_message_text("Введите пароль одним сообщением. Команда для отмены: /cancelpass")

        elif data == "kb_pass_cancel":
            context.user_data.pop("await_password_for", None)
            await q.edit_message_text("Ввод пароля отменён.")

        elif data == "kb_upload_mode":
            # Только для админа
            if self.admins and (not update.effective_user or update.effective_user.id not in self.admins):
                await q.edit_message_text("⛔ Доступно только администратору.")
                return
            context.user_data["await_kb_upload"] = True
            await q.edit_message_text(
                "Режим загрузки в БЗ активирован. Пришлите фото/документы одним или несколькими сообщениями.\n"
                "Команда для выхода: /cancelupload"
            )

        # --- Models / Modes / Dialog navigation ---
        elif data.startswith("set_model:"):
            m = data.split(":", 1)[1]
            context.user_data["model"] = m  # персонально для чата/пользователя
            await q.edit_message_text(f"Модель установлена: {m}")

        elif data == "noop":
            await q.answer("Эта модель уже выбрана.", show_alert=False)

        elif data.startswith("goto_dialog:"):
            try:
                target = int(data.split(":")[1])
            except ValueError:
                return
            db = self._get_db()
            chat_id = update.effective_chat.id
            db.query(Conversation).filter_by(chat_id=chat_id, is_active=True).update({"is_active": False})
            c = db.query(Conversation).filter_by(chat_id=chat_id, id=target).first()
            if c:
                c.is_active = True
                db.commit()
                await q.edit_message_text(f"✅ Активирован диалог #{c.id} ({c.title}).")
            else:
                await q.edit_message_text("Диалог не найден.")

        elif data.startswith("set_mode:"):
            mode = data.split(":", 1)[1]
            context.user_data["style"] = mode
            await q.edit_message_text(f"Стиль установлен: {STYLE_LABELS.get(mode, 'Профессиональный')}")

        # --- Delete dialogs ---
        elif data.startswith("ask_del_all"):
            rows = [
                [InlineKeyboardButton("✅ Да, удалить все неактивные", callback_data="do_del_all")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_del")],
            ]
            await q.edit_message_text("Подтвердите удаление всех НЕактивных диалогов:", reply_markup=InlineKeyboardMarkup(rows))

        elif data.startswith("do_del_all"):
            db = self._get_db()
            chat_id = update.effective_chat.id
            to_del = db.query(Conversation).filter_by(chat_id=chat_id, is_active=False).all()
            n = len(to_del)
            for c in to_del:
                db.delete(c)
            db.commit()
            await q.edit_message_text(f"🧹 Удалено неактивных диалогов: {n}")

        elif data.startswith("ask_del:"):
            try:
                cid = int(data.split(":")[1])
            except ValueError:
                await q.edit_message_text("Некорректный id диалога.")
                return
            rows = [
                [InlineKeyboardButton("✅ Да, удалить", callback_data=f"do_del:{cid}")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_del")],
            ]
            await q.edit_message_text(f"Удалить диалог #{cid}?", reply_markup=InlineKeyboardMarkup(rows))

        elif data.startswith("do_del:"):
            try:
                cid = int(data.split(":")[1])
            except ValueError:
                await q.edit_message_text("Некорректный id диалога.")
                return

            db = self._get_db()
            chat_id = update.effective_chat.id
            c = db.query(Conversation).filter_by(chat_id=chat_id, id=cid).first()
            if not c:
                await q.edit_message_text("Диалог не найден.")
                return

            was_active = bool(getattr(c, "is_active", False))
            db.delete(c)
            db.commit()

            if was_active:
                next_conv = (
                    db.query(Conversation)
                    .filter_by(chat_id=chat_id)
                    .order_by(Conversation.id.desc())
                    .first()
                )
                if next_conv:
                    next_conv.is_active = True
                    db.commit()
                    await q.edit_message_text(f"🗑️ Диалог #{cid} удалён. Активирован диалог #{next_conv.id} ({next_conv.title}).")
                else:
                    nc = Conversation(chat_id=chat_id, title="Диалог", is_active=True)
                    db.add(nc)
                    db.commit()
                    await q.edit_message_text(f"🗑️ Диалог #{cid} удалён. Создан новый пустой диалог.")
            else:
                await q.edit_message_text(f"🗑️ Диалог #{cid} удалён.")

        elif data == "cancel_del":
            await q.edit_message_text("Удаление отменено.")

    # ---------- KB sync ----------
    async def _kb_sync_internal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        from bot.knowledge_base.indexer import sync_disk_to_db
        db = SessionLocal()
        stats = {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0}
        try:
            stats = sync_disk_to_db(
                db,
                self.settings.yandex_disk_token,
                self.settings.yandex_root_path,
            )
            msg = (
                "Готово.\n"
                f"➕ Добавлено: {stats.get('added', 0)}\n"
                f"♻️ Обновлено: {stats.get('updated', 0)}\n"
                f"🗑️ Удалено: {stats.get('deleted', 0)}\n"
                f"✅ Без изменений: {stats.get('unchanged', 0)}"
            )
            await update.effective_chat.send_message(msg)
        except Exception as e:
            await update.effective_chat.send_message(f"Ошибка синхронизации: {e}")
            logger.exception("KB sync failed: %s", e)
        finally:
            db.close()

    # ---------- Проверка: нужен ли пароль для PDF ----------
    async def _needs_password_for_doc(self, doc: Document) -> bool:
        """
        Возвращает True, если документ – PDF и похоже зашифрован.
        Проверка без внешних зависимостей: ищем маркер '/Encrypt' в первых ~2 МБ.
        """
        try:
            mime = (doc.mime or "").lower()
            if mime != "application/pdf":
                return False

            import tempfile
            y = yadisk.YaDisk(token=self.settings.yandex_disk_token)

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tf:
                tmp = tf.name
            try:
                y.download(doc.path, tmp)
                with open(tmp, "rb") as f:
                    head = f.read(2_000_000)
                return b"/Encrypt" in head
            finally:
                with suppress(Exception):
                    os.unlink(tmp)
        except Exception as e:
            logger.warning("Не удалось проверить шифрование PDF (%s): %s", getattr(doc, "path", "?"), e)
            # Если не смогли проверить — не блокируем пользователя запросом пароля
            return False

    # ---------- Cancel KB upload ----------
    @only_allowed
    async def on_cancel_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.pop("await_kb_upload", None)
        await update.message.reply_text("Режим загрузки в БЗ отключён.")

    # ---------- KB passwords ----------
    @only_allowed
    async def on_cancel_pass(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.pop("await_password_for", None)
        await update.message.reply_text("Ввод пароля отменён.")

    # ---------- Voice messages ----------
    @only_allowed
    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        voice = update.message.voice
        if not voice:
            return
        file = await context.bot.get_file(voice.file_id)

        # Сохраняем во временный .ogg (Telegram voice = OGG/Opus)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tf:
            tmp_ogg = tf.name
        try:
            await file.download_to_drive(custom_path=tmp_ogg)
        except Exception as e:
            await update.message.reply_text(f"Не удалось скачать голосовое: {e}")
            return

        # Пытаемся распознать
        text: Optional[str] = None
        try:
            async with ChatActionSender(
                action=ChatAction.RECORD_VOICE,
                chat_id=update.effective_chat.id,
                bot=context.bot,
            ):
                text = await asyncio.to_thread(self.openai.transcribe, tmp_ogg)
        except Exception:
            # fallback: mp3 через pydub (требует ffmpeg)
            try:
                from pydub import AudioSegment
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tf2:
                    tmp_mp3 = tf2.name
                audio = AudioSegment.from_file(tmp_ogg)
                audio.export(tmp_mp3, format="mp3")
                async with ChatActionSender(
                    action=ChatAction.RECORD_VOICE,
                    chat_id=update.effective_chat.id,
                    bot=context.bot,
                ):
                    text = await asyncio.to_thread(self.openai.transcribe, tmp_mp3)
                os.unlink(tmp_mp3)
            except Exception as e2:
                await update.message.reply_text(
                    "Не удалось распознать голосовое. "
                    "Попробуйте прислать файл в формате mp3/m4a/wav или установите ffmpeg в образ."
                )
                logger.exception("Voice STT failed: %s", e2)
                with suppress(Exception):
                    os.unlink(tmp_ogg)
                return

        with suppress(Exception):
            os.unlink(tmp_ogg)

        if text:
            update.message.text = text
            await self.on_text(update, context)

    # ---------- Photos/Documents ----------
    @only_allowed
    async def on_file_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Если активирован режим загрузки в БЗ — принимаем файлы
        awaiting_upload = context.user_data.get("await_kb_upload")
        is_admin = (not self.admins) or (update.effective_user and update.effective_user.id in self.admins)

        if awaiting_upload and is_admin:
            try:
                saved, remote = await self._save_incoming_to_yadisk(update, context)
                await update.message.reply_text(f"📥 Загружено в БЗ: {remote}\nЗапускаю синхронизацию…")
                await self._kb_sync_internal(update, context)
            except Exception as e:
                await update.message.reply_text(f"Ошибка загрузки в БЗ: {e}")
                logger.exception("KB upload failed: %s", e)
            return

        tip = "Чтобы добавить в БЗ, откройте /kb → «📥 Добавить из чата» (доступно администратору)."
        await update.message.reply_text(tip)

    async def _save_incoming_to_yadisk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Tuple[str, str]:
        """
        Сохраняет фото/документ с сообщения в Я.Диск.
        Возвращает (local_temp_path, remote_path).
        """
        y = yadisk.YaDisk(token=self.settings.yandex_disk_token)

        if update.message.document:
            doc = update.message.document
            file = await context.bot.get_file(doc.file_id)
            filename = doc.file_name or f"file_{doc.file_unique_id}"
            ext = os.path.splitext(filename)[1] or ".bin"
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
                local = tf.name
            await file.download_to_drive(custom_path=local)
        elif update.message.photo:
            ph = update.message.photo[-1]  # самое большое качество
            file = await context.bot.get_file(ph.file_id)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"photo_{ts}.jpg"
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
                local = tf.name
            await file.download_to_drive(custom_path=local)
        else:
            raise ValueError("Нет поддерживаемого вложения")

        root = self.settings.yandex_root_path.strip()
        if not root.startswith("/"):
            root = "/" + root
        remote = f"disk:{root}/{filename}"

        y.upload(local_path=local, path=remote, overwrite=True)

        return local, remote

    # ---------- Text handler ----------
    @only_allowed
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        conv = self._get_active_conv(chat_id, db)

        # 1) Режим ввода пароля
        awaiting: Optional[int] = context.user_data.get("await_password_for")
        if awaiting is not None:
            pwd = (update.message.text or "").strip()
            if not pwd:
                await update.message.reply_text("Пустой пароль. Повторите ввод или /cancelpass")
                return
            kb_passwords: Dict[int, str] = context.user_data.get("kb_passwords", {}) or {}
            kb_passwords[awaiting] = pwd
            context.user_data["kb_passwords"] = kb_passwords
            context.user_data.pop("await_password_for", None)
            await update.message.reply_text("✅ Пароль сохранён для выбранного документа.")
            return

        # 2) Формируем стиль и подсказку
        kb_enabled = context.user_data.get("kb_enabled", True)
        selected_ids = context.user_data.get("kb_selected_ids", set())
        selected_docs: List[Document] = []
        if kb_enabled and selected_ids:
            selected_docs = db.query(Document).filter(Document.id.in_(list(selected_ids))).all()

        style = context.user_data.get("style", "pro")
        sys_hint, temp = style_system_hint(style)

        kb_hint = ""
        if selected_docs:
            titles = ", ".join([d.title for d in selected_docs][:10])
            kb_hint = f" Учитывай информацию из документов: {titles}."

        # 3) Заголовок диалога
        self._ensure_conv_title(conv, update.message.text or "", db)

        # 4) Персональная модель
        user_model = context.user_data.get("model", self.openai.model)

        # 5) Запрос к OpenAI — в поток
        prompt = (update.message.text or "").strip()
        messages = [
            {"role": "system", "content": (sys_hint + kb_hint).strip()},
            {"role": "user", "content": prompt},
        ]

        try:
            async with ChatActionSender(
                action=ChatAction.TYPING,
                chat_id=update.effective_chat.id,
                bot=context.bot,
            ):
                ans = await asyncio.to_thread(
                    self.openai.chat,
                    messages,
                    temperature=temp,
                    max_output_tokens=4096,
                    model=user_model,
                )
        except Exception as e:
            await update.message.reply_text(f"Ошибка обращения к OpenAI: {e}")
            return

        await update.message.reply_text(ans or "Пустой ответ.")
