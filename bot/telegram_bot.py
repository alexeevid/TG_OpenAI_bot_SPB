import logging
from functools import wraps
from typing import Optional, List, Tuple, Dict
from io import BytesIO
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackContext,
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

# ----- Access decorator -----
def only_allowed(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if self.allowed and uid not in self.allowed:
            await update.effective_message.reply_text("⛔ Доступ ограничен.")
            return
        return await func(self, update, context)
    return wrapper

# ----- Style presets -----
STYLE_LABELS = {
    "pro": "Профессиональный",
    "expert": "Экспертный",
    "user": "Пользовательский",
    "ceo": "СЕО",
}

def style_system_hint(style: str) -> Tuple[str, float]:
    """
    Возвращает (system_prompt, temperature) для выбранного стиля.
    """
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

    # ----- Wiring -----
    def install(self, app: Application):
        app.add_handler(CommandHandler("start", self.on_start))
        app.add_handler(CommandHandler("help", self.on_help))
        app.add_handler(CommandHandler("reset", self.on_reset))
        app.add_handler(CommandHandler("stats", self.on_stats))
        app.add_handler(CommandHandler("kb", self.on_kb))
        app.add_handler(CommandHandler("model", self.on_model))
        app.add_handler(CommandHandler("dialogs", self.on_dialogs))
        app.add_handler(CommandHandler("dialog", self.on_dialog_select))

        # New: modes & images & KB passwords
        app.add_handler(CommandHandler("mode", self.on_mode))
        app.add_handler(CommandHandler("img", self.on_img))
        app.add_handler(CommandHandler("cancelpass", self.on_cancel_pass))

        app.add_handler(CallbackQueryHandler(self.on_callback))

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        # Commands menu
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
        ]
        try:
            await app.bot.set_my_commands(cmds)
        except Exception as e:
            logger.warning("Failed to set commands: %s", e)

    # ---------- DB helpers ----------
    def _get_db(self) -> Session:
        return SessionLocal()

    def _get_active_conv(self, chat_id: int, db: Session) -> Conversation:
        conv = db.query(Conversation).filter_by(chat_id=chat_id, is_active=True).order_by(Conversation.id.desc()).first()
        if not conv:
            conv = Conversation(chat_id=chat_id, title="Диалог")
            db.add(conv); db.commit(); db.refresh(conv)
        return conv

    # ---------- Title helpers ----------
    @staticmethod
    def _short_title_from_text(text: str, limit: int = 48) -> str:
        base = (text or "").strip().splitlines()[0]
        base = " ".join(base.split())
        return (base[:limit] + "…") if len(base) > limit else base

    def _ensure_conv_title(self, conv: Conversation, first_user_text: str, db: Session):
        """Если заголовок ещё стандартный — автоименуем. Обновляем метку upd на каждом сообщении."""
        base = conv.title or "Диалог"
        created = conv.created_at.strftime("%Y-%m-%d")
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        if base == "Диалог":
            short = self._short_title_from_text(first_user_text) or "Диалог"
            conv.title = f"{short} · {created} · upd {now}"
        else:
            # обновляем только хвост с меткой upd
            parts = base.split(" · ")
            if len(parts) >= 2:
                conv.title = " · ".join(parts[:2] + [f"upd {now}"])
            else:
                conv.title = f"{base} · upd {now}"
        db.add(conv); db.commit()

    # ---------- Commands ----------
    @only_allowed
    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет! Я готов к работе.\n"
            "Команды: /help, /reset, /stats, /kb, /model, /dialogs, /img, /mode"
        )

    @only_allowed
    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "/reset — сброс контекста\n"
            "/stats — статистика\n"
            "/kb — база знаний (включить/исключить документы, пароли)\n"
            "/model — выбор модели OpenAI\n"
            "/mode — выбрать стиль ответов (Профессиональный/Экспертный/Пользовательский/СЕО)\n"
            "/dialogs — список диалогов, /dialog <id> — вернуться\n"
            "/img <описание> — сгенерировать изображение"
        )

    @only_allowed
    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        db.query(Conversation).filter_by(chat_id=chat_id, is_active=True).update({"is_active": False})
        db.commit()
        newc = Conversation(chat_id=chat_id, title="Диалог")
        db.add(newc); db.commit()
        await update.message.reply_text("🔄 Новый диалог создан. Контекст очищен.")
        context.user_data.pop("kb_enabled", None)
        context.user_data.pop("kb_selected_ids", None)
        context.user_data.pop("kb_passwords", None)
        context.user_data.pop("await_password_for", None)
        context.user_data.pop("style", None)

    @only_allowed
    async def on_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        conv = self._get_active_conv(chat_id, db)
        docs = context.user_data.get("kb_selected_ids", set()) or set()
        kb_enabled = context.user_data.get("kb_enabled", True)
        style = context.user_data.get("style", "pro")
        style_label = STYLE_LABELS.get(style, "Профессиональный")

        title = conv.title or "Диалог"
        names = []
        if docs:
            q = db.query(Document).filter(Document.id.in_(list(docs))).all()
            names = [d.title for d in q]

        text = (
            f"📊 Статистика:\n"
            f"- Диалог: {title}\n"
            f"- Модель: {self.openai.model}\n"
            f"- Стиль: {style_label}\n"
            f"- База знаний: {'включена' if kb_enabled else 'выключена'}\n"
            f"- Документов выбрано: {len(docs)}"
        )
        if names:
            text += "\n- В контексте: " + ", ".join(names[:10])
            if len(names) > 10:
                text += f" и ещё {len(names) - 10}…"

        await update.message.reply_text(text)

    # ----- KB -----
    @only_allowed
    async def on_kb(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        self._get_active_conv(chat_id, db)  # ensure exists

        kb_enabled = context.user_data.get("kb_enabled", True)
        selected = context.user_data.get("kb_selected_ids", set())
        docs = db.query(Document).order_by(Document.id.asc()).limit(30).all()

        rows = []
        for d in docs:
            mark = "✅" if d.id in selected else "➕"
            rows.append([InlineKeyboardButton(f"{mark} {d.title}", callback_data=f"kb_toggle:{d.id}")])

        # Admin-only sync button
        if update.effective_user and update.effective_user.id in self.admins:
            rows.append([InlineKeyboardButton("🔄 Синхронизировать с Я.Диском", callback_data="kb_sync")])

        rows.append([InlineKeyboardButton(("🔕 Отключить БЗ" if kb_enabled else "🔔 Включить БЗ"), callback_data="kb_toggle_enabled")])
        rows.append([InlineKeyboardButton("🔐 Указать пароли для выбранных", callback_data="kb_pass_menu")])

        await update.message.reply_text(
            f"База знаний: {'включена' if kb_enabled else 'выключена'}.\n"
            "Выберите документы для контекста (до 30 показано).",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    # ----- Models -----
    @only_allowed
    async def on_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        models = self.openai.list_models()
        prefer = [m for m in models if any(x in m for x in ["gpt-4o", "gpt-4.1", "gpt-4", "gpt-3.5"])]
        items = prefer[:30] if prefer else models[:30]
        rows = [[InlineKeyboardButton(m, callback_data=f"set_model:{m}")] for m in items]
        await update.message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(rows))

    # ----- Modes -----
    @only_allowed
    async def on_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        rows = [
            [InlineKeyboardButton("Профессиональный", callback_data="set_mode:pro")],
            [InlineKeyboardButton("Экспертный", callback_data="set_mode:expert")],
            [InlineKeyboardButton("Пользовательский", callback_data="set_mode:user")],
            [InlineKeyboardButton("СЕО", callback_data="set_mode:ceo")],
        ]
        await update.message.reply_text("Выберите стиль ответов:", reply_markup=InlineKeyboardMarkup(rows))

    # ----- Images -----
    @only_allowed
    async def on_img(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        prompt = " ".join(context.args) if context.args else ""
        if not prompt and update.message and update.message.reply_to_message:
            prompt = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        prompt = (prompt or "").strip()
        if not prompt:
            await update.message.reply_text("Уточните описание, например: `/img логотип в стиле минимализм`", parse_mode="Markdown")
            return

        try:
            png = self.openai.generate_image(prompt, size="1024x1024")
            bio = BytesIO(png); bio.name = "image.png"; bio.seek(0)
            await update.message.reply_photo(photo=InputFile(bio, filename="image.png"), caption=prompt)
        except Exception as e:
            await update.message.reply_text(f"Ошибка генерации изображения: {e}")

    # ----- Dialogs -----
    @only_allowed
    async def on_dialogs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        items = db.query(Conversation).filter_by(chat_id=chat_id).order_by(Conversation.id.desc()).limit(10).all()
        if not items:
            await update.message.reply_text("Нет сохранённых диалогов.")
            return
        rows = [[InlineKeyboardButton(f"#{c.id} {c.title}", callback_data=f"goto_dialog:{c.id}") ] for c in items]
        await update.message.reply_text("Выберите диалог:", reply_markup=InlineKeyboardMarkup(rows))

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

    # ----- Callbacks -----
    @only_allowed
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        data = q.data or ""

        if data.startswith("kb_toggle:"):
            doc_id = int(data.split(":")[1])
            selected = context.user_data.get("kb_selected_ids", set())
            if doc_id in selected:
                selected.remove(doc_id)
            else:
                selected.add(doc_id)
            context.user_data["kb_selected_ids"] = selected
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("Изменения применены. Нажмите /kb, чтобы обновить список.")

        elif data == "kb_toggle_enabled":
            cur = context.user_data.get("kb_enabled", True)
            context.user_data["kb_enabled"] = not cur
            await q.edit_message_text(f"База знаний: {'включена' if not cur else 'выключена'}. Нажмите /kb, чтобы обновить.")

        elif data == "kb_sync":
            if update.effective_user and update.effective_user.id in self.admins:
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

        elif data.startswith("set_model:"):
            m = data.split(":", 1)[1]
            self.openai.set_model(m)
            await q.edit_message_text(f"Модель установлена: {m}")

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
                c.is_active = True; db.commit()
                await q.edit_message_text(f"✅ Активирован диалог #{c.id} ({c.title}).")
            else:
                await q.edit_message_text("Диалог не найден.")

        elif data.startswith("set_mode:"):
            mode = data.split(":", 1)[1]
            context.user_data["style"] = mode
            await q.edit_message_text(f"Стиль установлен: {STYLE_LABELS.get(mode, 'Профессиональный')}")

    async def _kb_sync_internal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        from bot.knowledge_base.indexer import sync_disk_to_db
        db = SessionLocal()
        added = 0
        try:
            added = sync_disk_to_db(db, self.settings.yandex_disk_token, self.settings.yandex_root_path)
            await update.effective_chat.send_message(f"Готово. Добавлено файлов: {added}")
        except Exception as e:
            await update.effective_chat.send_message(f"Ошибка синхронизации: {e}")
        finally:
            db.close()

    # ----- KB passwords -----
    @only_allowed
    async def on_cancel_pass(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.pop("await_password_for", None)
        await update.message.reply_text("Ввод пароля отменён.")

    # ----- Text handler -----
    @only_allowed
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = self._get_db()
        chat_id = update.effective_chat.id
        conv = self._get_active_conv(chat_id, db)

        # 1) Обработка пароля для документа (интерактивный режим)
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

        # 3) Обновим заголовок диалога (первое/каждое сообщение)
        self._ensure_conv_title(conv, update.message.text or "", db)

        # 4) Запрос к OpenAI
        prompt = (update.message.text or "").strip()
        messages = [
            {"role": "system", "content": (sys_hint + kb_hint).strip()},
            {"role": "user", "content": prompt}
        ]

        try:
            ans = self.openai.chat(messages, temperature=temp, max_output_tokens=2048)
        except Exception as e:
            await update.message.reply_text(f"Ошибка обращения к OpenAI: {e}")
            return

        await update.message.reply_text(ans or "Пустой ответ.")
