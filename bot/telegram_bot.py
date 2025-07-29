# bot/telegram_bot.py
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import textwrap
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    BotCommand,
    InputFile,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

# Не обязательный модуль Базы знаний — подключаем условно
try:
    # Предполагается ваша реализация внутри bot/knowledge_base/*
    from bot.knowledge_base.indexer import KnowledgeBase  # type: ignore
    KB_AVAILABLE = True
except Exception:
    KnowledgeBase = None  # type: ignore
    KB_AVAILABLE = False

logger = logging.getLogger(__name__)


# Стили ответа
STYLE_LABELS = {
    "Pro": "Профессиональный",
    "Expert": "Экспертный",
    "User": "Пользовательский",
    "CEO": "СЕО",
}
STYLE_ORDER = ["Pro", "Expert", "User", "CEO"]


class ChatGPTTelegramBot:
    """
    Основная обертка над telegram.ext, хранит пользовательское состояние в памяти
    (персональная модель, стиль ответа, текущий диалог, выбранные документы и т.д.).
    """

    def __init__(self, openai, settings):
        self.openai = openai
        self.settings = settings

        # Персональное состояние пользователей в памяти
        # user_id -> state dict
        self.state: Dict[int, Dict[str, Any]] = {}

        # Простая автоинкрементация идентификаторов диалогов в памяти
        self._dialog_seq: int = 1

        # Экземпляр БЗ (если доступен)
        self.kb = KnowledgeBase(settings) if KB_AVAILABLE else None

    # ------------------------------- Утилиты состояния ------------------------

    def _ensure_user(self, user_id: int) -> Dict[str, Any]:
        st = self.state.get(user_id)
        if not st:
            st = {
                "model": self.openai.get_user_model(user_id) or getattr(self.settings, "openai_model", None) or "gpt-4o",
                "style": "Pro",
                "kb_enabled": False,
                "kb_selected_docs": [],  # список doc_id или путей — зависит от вашей реализации KB
                "dialogs": {},           # dialog_id -> {"title": str, "created_at": ..., "updated_at": ...}
                "current_dialog": None,  # dialog_id
                "await_password_for_doc": None,  # запрос пароля для зашифрованного документа
            }
            # создадим первый диалог
            did = self._next_dialog_id()
            st["dialogs"][did] = self._make_dialog_meta("Диалог")
            st["current_dialog"] = did
            self.state[user_id] = st
        return st

    def _next_dialog_id(self) -> int:
        v = self._dialog_seq
        self._dialog_seq += 1
        return v

    def _make_dialog_meta(self, base_title: str) -> Dict[str, Any]:
        now = dt.datetime.now()
        return {
            "title": base_title,
            "created_at": now,
            "updated_at": now,
        }

    def _touch_dialog(self, st: Dict[str, Any]) -> None:
        did = st.get("current_dialog")
        if not did:
            return
        meta = st["dialogs"].get(did)
        if meta:
            meta["updated_at"] = dt.datetime.now()

    def _format_dialog_title(self, meta: Dict[str, Any]) -> str:
        created = meta.get("created_at")
        updated = meta.get("updated_at")
        created_s = created.strftime("%Y-%m-%d %H:%M") if created else "-"
        updated_s = updated.strftime("%Y-%m-%d %H:%M") if updated else "-"
        return f"{meta.get('title','Диалог')}  🕒 {created_s} • ✎ {updated_s}"

    # ------------------------------- post_init меню ---------------------------

    async def _set_global_commands(self, app: Application) -> None:
        cmds = [
            BotCommand("help", "Помощь"),
            BotCommand("reset", "Сброс контекста"),
            BotCommand("stats", "Статистика"),
            BotCommand("kb", "База знаний"),
            BotCommand("model", "Выбор модели"),
            BotCommand("mode", "Стиль ответов"),
            BotCommand("dialogs", "Диалоги"),
            BotCommand("img", "Сгенерировать изображение"),
            BotCommand("web", "Веб‑поиск"),
        ]
        # указываем команды по умолчанию и для приватных чатов
        try:
            await app.bot.set_my_commands(cmds)
        except Exception as e:
            logger.warning("Не удалось установить команды бота: %s", e)

    # ------------------------------- Установка хендлеров ----------------------

    def install(self, app: Application) -> None:
        # Команды
        app.add_handler(CommandHandler("start", self.on_start))
        app.add_handler(CommandHandler("help", self.on_help))
        app.add_handler(CommandHandler("reset", self.on_reset))
        app.add_handler(CommandHandler("stats", self.on_stats))
        app.add_handler(CommandHandler("kb", self.on_kb))
        app.add_handler(CommandHandler("model", self.on_model))
        app.add_handler(CommandHandler("mode", self.on_mode))
        app.add_handler(CommandHandler("dialogs", self.on_dialogs))
        app.add_handler(CommandHandler("img", self.on_image))
        app.add_handler(CommandHandler("web", self.on_web))

        # Callback’и от инлайн‑кнопок
        app.add_handler(CallbackQueryHandler(self.on_callback))

        # Сообщения
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        # Правильный post_init: присваиваем колбэк, а не вызываем
        async def _post_init(application: Application) -> None:
            await self._set_global_commands(application)

        app.post_init = _post_init

    # ------------------------------- Команды ---------------------------------

    async def on_start(self, update: Update, context: CallbackContext) -> None:
        user = update.effective_user
        self._ensure_user(user.id)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Привет! Я готов к работе.\nКоманды: /help, /reset, /stats, /kb, /model, /mode, /dialogs, /img, /web",
        )

    async def on_help(self, update: Update, context: CallbackContext) -> None:
        text = (
            "/reset — сброс контекста\n"
            "/stats — статистика\n"
            "/kb — база знаний (включить/исключить документы)\n"
            "/model — выбор модели OpenAI\n"
            "/mode — стиль ответов\n"
            "/dialogs — список диалогов\n"
            "/img — сгенерировать изображение из описания\n"
            "/web — веб‑поиск\n"
        )
        await context.bot.send_message(update.effective_chat.id, text)

    async def on_reset(self, update: Update, context: CallbackContext) -> None:
        user_id = update.effective_user.id
        st = self._ensure_user(user_id)
        # создаём новый диалог
        did = self._next_dialog_id()
        st["dialogs"][did] = self._make_dialog_meta("Диалог")
        st["current_dialog"] = did
        await context.bot.send_message(update.effective_chat.id, "🔄 Новый диалог создан. Контекст очищен.")

    async def on_stats(self, update: Update, context: CallbackContext) -> None:
        user_id = update.effective_user.id
        st = self._ensure_user(user_id)
        did = st.get("current_dialog")
        meta = st["dialogs"].get(did) if did else None
        dialog_title = meta["title"] if meta else "Диалог"

        model = st.get("model") or self.openai.get_user_model(user_id) or self.openai.default_model
        style = st.get("style", "Pro")
        kb_on = st.get("kb_enabled", False)
        docs = st.get("kb_selected_docs", [])
        docs_names = ", ".join(self._kb_doc_display_name(d) for d in docs) if docs else "—"

        text = (
            "📊 Статистика:\n"
            f"- Диалог: {dialog_title}\n"
            f"- Модель: {model}\n"
            f"- Стиль: {STYLE_LABELS.get(style, style)}\n"
            f"- База знаний: {'включена' if kb_on else 'выключена'}\n"
            f"- Документов выбрано: {len(docs)}\n"
        )
        if docs and len(docs_names) <= 900:
            text += f"- В контексте: {docs_names}\n"

        await context.bot.send_message(update.effective_chat.id, text)

    async def on_model(self, update: Update, context: CallbackContext) -> None:
        user_id = update.effective_user.id
        st = self._ensure_user(user_id)
        current = st.get("model") or self.openai.get_user_model(user_id) or self.openai.default_model

        try:
            models = self.openai.list_models_for_user(user_id)
        except Exception as e:
            await context.bot.send_message(update.effective_chat.id, f"Ошибка запроса списка моделей: {e}")
            return

        # Делим на строки по 2-3 кнопки
        buttons: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        for m in models[:36]:
            label = f"{'✅ ' if m == current else ''}{m}"
            row.append(InlineKeyboardButton(label, callback_data=f"model:set:{m}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        kb = InlineKeyboardMarkup(buttons)
        await context.bot.send_message(update.effective_chat.id, "Выберите модель:", reply_markup=kb)

    async def on_mode(self, update: Update, context: CallbackContext) -> None:
        user_id = update.effective_user.id
        st = self._ensure_user(user_id)
        current = st.get("style", "Pro")

        buttons: List[List[InlineKeyboardButton]] = []
        for style in STYLE_ORDER:
            label = f"{'✅ ' if style == current else ''}{STYLE_LABELS[style]}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"mode:set:{style}")])

        await context.bot.send_message(update.effective_chat.id, "Выберите стиль ответов:", reply_markup=InlineKeyboardMarkup(buttons))

    async def on_dialogs(self, update: Update, context: CallbackContext) -> None:
        user_id = update.effective_user.id
        st = self._ensure_user(user_id)
        did_current = st.get("current_dialog")
        dialogs: Dict[int, Dict[str, Any]] = st.get("dialogs", {})

        if not dialogs:
            await context.bot.send_message(update.effective_chat.id, "Диалогов пока нет.")
            return

        def short(name: str) -> str:
            return name if len(name) <= 30 else name[:27] + "…"

        rows: List[List[InlineKeyboardButton]] = []
        for did, meta in sorted(dialogs.items(), key=lambda kv: kv[1].get("updated_at") or kv[1].get("created_at"), reverse=True):
            title_line = self._format_dialog_title(meta)
            marker = "⭐ " if did == did_current else ""
            # В одну строку: Открыть / Удалить
            rows.append([
                InlineKeyboardButton(f"{marker}{short(title_line)}", callback_data=f"dialog:open:{did}"),
                InlineKeyboardButton("🗑️", callback_data=f"dialog:del:{did}"),
            ])

        rows.append([InlineKeyboardButton("➕ Новый диалог", callback_data="dialog:new")])

        await context.bot.send_message(update.effective_chat.id, "Выберите диалог:", reply_markup=InlineKeyboardMarkup(rows))

    async def on_kb(self, update: Update, context: CallbackContext) -> None:
        user_id = update.effective_user.id
        st = self._ensure_user(user_id)
        kb_on = st.get("kb_enabled", False)

        if not KB_AVAILABLE or not self.kb:
            await context.bot.send_message(
                update.effective_chat.id,
                "Модуль базы знаний недоступен в этой сборке. Проверьте, что папка bot/knowledge_base/* присутствует в деплое.",
            )
            return

        # Сформируем меню KB
        btns = [
            [InlineKeyboardButton("🔁 Синхронизировать", callback_data="kb:sync")],
            [InlineKeyboardButton("📄 Выбрать документы", callback_data="kb:choose")],
            [InlineKeyboardButton(("🟢 Выключить БЗ" if kb_on else "⚪ Включить БЗ"), callback_data="kb:toggle")],
        ]
        await context.bot.send_message(update.effective_chat.id, "База знаний:", reply_markup=InlineKeyboardMarkup(btns))

    async def on_image(self, update: Update, context: CallbackContext) -> None:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        st = self._ensure_user(user_id)

        args = update.message.text.split(maxsplit=1)
        if len(args) < 2:
            await context.bot.send_message(chat_id, "Укажите описание после команды. Пример: /img белый кот на синем фоне, минимализм")
            return
        prompt = args[1].strip()
        model = self.openai.get_image_model()

        # Индикатор загрузки фото
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)

        try:
            img_bytes, used_prompt = await asyncio.to_thread(self.openai.generate_image, prompt, model)
        except Exception as e:
            logger.exception("Image generation failed: %s", e)
            await context.bot.send_message(chat_id, f"Ошибка генерации изображения: {e}")
            return

        with tempfile.NamedTemporaryFile("wb", suffix=".png", delete=False) as tmp:
            tmp.write(img_bytes)
            tmp_path = tmp.name

        try:
            caption = f"🖼️ Изображение по запросу:\n{used_prompt}"
            await context.bot.send_photo(chat_id, photo=InputFile(tmp_path), caption=caption)
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    async def on_web(self, update: Update, context: CallbackContext) -> None:
        chat_id = update.effective_chat.id
        args = update.message.text.split(maxsplit=1)
        if len(args) < 2:
            await context.bot.send_message(chat_id, "Укажите запрос после команды. Пример: /web дисциплинированный эджайл")
            return
        query = args[1].strip()

        # Индикатор набора
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

        try:
            answer, links = await asyncio.to_thread(self.openai.web_search, query)
        except Exception as e:
            await context.bot.send_message(chat_id, f"Ошибка веб‑поиска: {e}")
            return

        text = answer.strip()
        if links:
            links_block = "\n".join(f"- {u}" for u in links[:8])
            text = f"{text}\n\n🔗 Источники:\n{links_block}"
        await context.bot.send_message(chat_id, text)

    # ------------------------------- Callbacks --------------------------------

    async def on_callback(self, update: Update, context: CallbackContext) -> None:
        query = update.callback_query
        data = query.data or ""
        user_id = query.from_user.id
        st = self._ensure_user(user_id)

        try:
            if data.startswith("model:set:"):
                model = data.split("model:set:", 1)[1]
                st["model"] = model
                self.openai.set_user_model(user_id, model)
                await query.answer("Модель переключена.")
                await query.edit_message_text(f"Выбрана модель: {model}")

            elif data.startswith("mode:set:"):
                style = data.split("mode:set:", 1)[1]
                if style in STYLE_LABELS:
                    st["style"] = style
                    await query.answer("Стиль обновлён.")
                    await query.edit_message_text(f"Выбран стиль: {STYLE_LABELS[style]}")
                else:
                    await query.answer("Неизвестный стиль.", show_alert=True)

            elif data == "dialog:new":
                did = self._next_dialog_id()
                st["dialogs"][did] = self._make_dialog_meta("Диалог")
                st["current_dialog"] = did
                await query.answer("Создан новый диалог.")
                await query.edit_message_text("Новый диалог создан и активирован.")

            elif data.startswith("dialog:open:"):
                did = int(data.split("dialog:open:", 1)[1])
                if did in st["dialogs"]:
                    st["current_dialog"] = did
                    await query.answer("Диалог открыт.")
                    await query.edit_message_text(f"Открыт диалог: {self._format_dialog_title(st['dialogs'][did])}")
                else:
                    await query.answer("Диалог не найден.", show_alert=True)

            elif data.startswith("dialog:del:"):
                did = int(data.split("dialog:del:", 1)[1])
                if did in st["dialogs"]:
                    # Если удаляем текущий — переключимся на любой другой
                    was_current = (st.get("current_dialog") == did)
                    del st["dialogs"][did]
                    if was_current:
                        if st["dialogs"]:
                            st["current_dialog"] = next(iter(st["dialogs"].keys()))
                        else:
                            # Создадим пустой
                            ndid = self._next_dialog_id()
                            st["dialogs"][ndid] = self._make_dialog_meta("Диалог")
                            st["current_dialog"] = ndid
                    await query.answer("Диалог удалён.")
                    await query.edit_message_text("Диалог удалён.")
                else:
                    await query.answer("Диалог не найден.", show_alert=True)

            elif data == "kb:toggle":
                st["kb_enabled"] = not st.get("kb_enabled", False)
                await query.answer("Готово.")
                await query.edit_message_text(f"База знаний теперь: {'включена' if st['kb_enabled'] else 'выключена'}")

            elif data == "kb:sync":
                if not KB_AVAILABLE or not self.kb:
                    await query.answer("Модуль БЗ недоступен.", show_alert=True)
                else:
                    await query.answer("Синхронизация…")
                    # Синхронизация может быть долгой — запустим в отдельном потоке
                    def _sync() -> Tuple[int, int, int, int]:
                        return self.kb.sync()  # ожидается, что вернёт added, updated, deleted, unchanged

                    try:
                        added, updated, deleted, unchanged = await asyncio.to_thread(_sync)
                        await query.edit_message_text(
                            f"Синхронизация завершена: добавлено {added}, обновлено {updated}, удалено {deleted}, без изменений {unchanged}."
                        )
                    except Exception as e:
                        logger.exception("KB sync failed: %s", e)
                        await query.edit_message_text(f"Ошибка синхронизации: {e}")

            elif data == "kb:choose":
                if not KB_AVAILABLE or not self.kb:
                    await query.answer("Модуль БЗ недоступен.", show_alert=True)
                else:
                    try:
                        docs = self.kb.list_documents()  # ожидается список dict’ов с полями id/title/encrypted
                    except Exception as e:
                        await query.edit_message_text(f"Ошибка получения списка документов: {e}")
                        return

                    if not docs:
                        await query.edit_message_text("В БЗ пока нет документов.")
                        return

                    rows: List[List[InlineKeyboardButton]] = []
                    sel = set(st.get("kb_selected_docs", []))
                    for d in docs[:48]:
                        did = d.get("id") or d.get("path") or str(d)
                        title = d.get("title") or d.get("path") or "Документ"
                        enc = d.get("encrypted", False)
                        mark = "✅ " if did in sel else ""
                        lock = " 🔒" if enc else ""
                        rows.append([
                            InlineKeyboardButton(f"{mark}{title}{lock}", callback_data=f"kb:doc:{did}")
                        ])
                    rows.append([InlineKeyboardButton("Готово", callback_data="kb:done")])
                    await query.edit_message_text("Выберите документы для контекста:", reply_markup=InlineKeyboardMarkup(rows))

            elif data.startswith("kb:doc:"):
                did = data.split("kb:doc:", 1)[1]
                sel: List[str] = st.get("kb_selected_docs", [])
                if did in sel:
                    sel.remove(did)
                else:
                    # Проверим — если документ зашифрован — запросим пароль
                    need_password = False
                    if KB_AVAILABLE and self.kb:
                        try:
                            meta = self.kb.get_document_meta(did)
                            need_password = bool(meta.get("encrypted"))
                        except Exception:
                            need_password = False
                    if need_password:
                        st["await_password_for_doc"] = did
                        await query.answer("Требуется пароль. Отправьте его отдельным сообщением.")
                        await query.edit_message_text("Документ зашифрован. Отправьте пароль сообщением (или /cancel).")
                        return
                    sel.append(did)
                    st["kb_selected_docs"] = sel
                await query.answer("Готово.")

            elif data == "kb:done":
                await query.answer("Готово.")
                await query.edit_message_text("Документы выбраны.")

            else:
                await query.answer()

        except Exception as e:
            logger.exception("Callback error: %s", e)
            await query.answer("Ошибка обработки нажатия.", show_alert=True)

    # ------------------------------- Сообщения --------------------------------

    async def on_text(self, update: Update, context: CallbackContext) -> None:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        st = self._ensure_user(user_id)

        # Перехват пароля для зашифрованного документа (при выборе в /kb)
        if st.get("await_password_for_doc"):
            doc_id = st["await_password_for_doc"]
            pwd = (update.message.text or "").strip()
            if pwd.lower() in {"/cancel", "отмена"}:
                st["await_password_for_doc"] = None
                await context.bot.send_message(chat_id, "Ввод пароля отменён.")
                return
            if KB_AVAILABLE and self.kb:
                try:
                    ok = self.kb.set_password_for_document(doc_id, pwd)
                    if ok:
                        # Добавим документ после успешной проверки
                        sel: List[str] = st.get("kb_selected_docs", [])
                        if doc_id not in sel:
                            sel.append(doc_id)
                            st["kb_selected_docs"] = sel
                        await context.bot.send_message(chat_id, "Пароль принят. Документ добавлен в контекст.")
                    else:
                        await context.bot.send_message(chat_id, "Неверный пароль. Попробуйте ещё раз или /cancel.")
                    return
                except Exception as e:
                    await context.bot.send_message(chat_id, f"Ошибка проверки пароля: {e}")
                    return
            else:
                await context.bot.send_message(chat_id, "Модуль БЗ недоступен в этой сборке.")
                return

        # Обычное сообщение — чат с моделью
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

        user_text = update.message.text
        model = st.get("model") or self.openai.get_user_model(user_id) or self.openai.default_model
        style = st.get("style", "Pro")
        did = st.get("current_dialog")

        # Если БЗ активна — подтянем выдержки
        kb_chunks: Optional[List[Tuple[int, str]]] = None
        if st.get("kb_enabled") and KB_AVAILABLE and self.kb:
            try:
                # ожидается, что вернёт список (doc_id, text_chunk)
                kb_chunks = self.kb.get_chunks_for_dialog(user_id, did, st.get("kb_selected_docs", []), limit_chars=4000)
            except Exception as e:
                logger.warning("KB chunks failed: %s", e)
                kb_chunks = None

        try:
            answer = await asyncio.to_thread(
                self.openai.chat,
                user_id,
                did or 0,
                user_text,
                model,
                style,
                kb_chunks,
            )
        except Exception as e:
            await context.bot.send_message(chat_id, f"Ошибка обращения к OpenAI: {e}")
            return

        self._touch_dialog(st)
        await context.bot.send_message(chat_id, answer)

    async def on_voice(self, update: Update, context: CallbackContext) -> None:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        st = self._ensure_user(user_id)

        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

        # Скачиваем голос
        vf = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            await vf.download_to_drive(custom_path=tmp.name)
            path = tmp.name

        try:
            text = await asyncio.to_thread(self.openai.transcribe, path)
        except Exception as e:
            await context.bot.send_message(chat_id, f"Не удалось распознать аудио: {e}")
            try:
                os.remove(path)
            except Exception:
                pass
            return

        # Сформируем ответ модели на транскрипт
        model = st.get("model") or self.openai.get_user_model(user_id) or self.openai.default_model
        style = st.get("style", "Pro")
        did = st.get("current_dialog")

        kb_chunks: Optional[List[Tuple[int, str]]] = None
        if st.get("kb_enabled") and KB_AVAILABLE and self.kb:
            try:
                kb_chunks = self.kb.get_chunks_for_dialog(user_id, did or 0, st.get("kb_selected_docs", []), limit_chars=4000)
            except Exception:
                kb_chunks = None

        try:
            answer = await asyncio.to_thread(
                self.openai.chat,
                user_id,
                did or 0,
                text or "",
                model,
                style,
                kb_chunks,
            )
        except Exception as e:
            answer = f"Ошибка ответа модели: {e}"

        finally:
            try:
                os.remove(path)
            except Exception:
                pass

        msg = f"🗣️ Расшифровка:\n{text or '—'}\n\n💬 Ответ:\n{answer}"
        await context.bot.send_message(chat_id, msg)

    async def on_photo(self, update: Update, context: CallbackContext) -> None:
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

        # Берём фото в наибольшем размере
        p = update.message.photo[-1]
        tf = await context.bot.get_file(p.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            await tf.download_to_drive(custom_path=tmp.name)
            path = tmp.name

        try:
            desc = await asyncio.to_thread(self.openai.describe_file, path)
            await context.bot.send_message(chat_id, desc)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    async def on_document(self, update: Update, context: CallbackContext) -> None:
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

        doc = update.message.document
        tf = await context.bot.get_file(doc.file_id)
        suffix = os.path.splitext(doc.file_name or "")[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            await tf.download_to_drive(custom_path=tmp.name)
            path = tmp.name

        try:
            desc = await asyncio.to_thread(self.openai.describe_file, path)
            # ВАЖНО: не добавляем в БЗ автоматически!
            await context.bot.send_message(chat_id, desc + "\n\nДобавить в БЗ можно через /kb → «Выбрать документы».")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    # ------------------------------- Вспомогательные --------------------------

    def _kb_doc_display_name(self, doc_id_or_meta: Any) -> str:
        """Презентация имени документа в stats — по возможности показываем title."""
        if isinstance(doc_id_or_meta, dict):
            return doc_id_or_meta.get("title") or doc_id_or_meta.get("path") or str(doc_id_or_meta)
        if KB_AVAILABLE and self.kb:
            try:
                meta = self.kb.get_document_meta(doc_id_or_meta)
                return meta.get("title") or meta.get("path") or str(doc_id_or_meta)
            except Exception:
                return str(doc_id_or_meta)
        return str(doc_id_or_meta)
