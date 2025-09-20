
"""
services/patch_ptb_commands.py (v4)
"""
from __future__ import annotations
import asyncio, logging, importlib, re, os, tempfile, time
from typing import Optional, Iterable, Any, List, Dict, Tuple

logger = logging.getLogger(__name__)
if not logger.handlers:
    h = logging.StreamHandler()
    fmt = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
    h.setFormatter(fmt)
    logger.addHandler(h)
logger.setLevel(logging.INFO)

try:
    from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
        ContextTypes, filters, Application, ApplicationHandlerStop
    )
except Exception as e:
    logger.warning("telegram not available; patch_ptb_commands inactive: %s", e)
    ApplicationBuilder = None  # type: ignore

_known_commands = {"start", "help", "dialogs", "dialog", "reset", "kb", "style", "model", "stats", "img", "search"}

_openai_helper = None
for _mod in ("bot.openai_helper", "openai_helper"):
    try:
        _openai_helper = importlib.import_module(_mod)
        break
    except Exception:
        continue

_db_mod = None
for _mod in ("bot.dialogs", "dialogs", "bot.db", "db"):
    try:
        _db_mod = importlib.import_module(_mod)
        break
    except Exception:
        continue

def _dialogs_keyboard(items: List[Dict[str, Any]], page: int = 0, page_size: int = 10) -> InlineKeyboardMarkup:
    start = page*page_size
    chunk = items[start:start+page_size]
    rows = []
    for d in chunk:
        did = str(d.get("id") or d.get("dialog_id") or d.get("did") or "")
        title = str(d.get("title") or d.get("name") or d.get("topic") or did)
        rows.append([InlineKeyboardButton(f"{title}", callback_data=f"dlg:sel:{did}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"dlg:page:{page-1}"))
    nav.append(InlineKeyboardButton("➕ Новый", callback_data="dlg:new:_"))
    nav.append(InlineKeyboardButton("✏️ Переименовать", callback_data="dlg:rename:_"))
    nav.append(InlineKeyboardButton("🗑 Удалить", callback_data="dlg:delete:_"))
    nav.append(InlineKeyboardButton("🔄 Обновить", callback_data=f"dlg:page:{page}"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)

def _kb_keyboard(entries: List[Dict[str, Any]], page: int = 0, page_size: int = 10, is_admin: bool=False) -> InlineKeyboardMarkup:
    start = page*page_size
    chunk = entries[start:start+page_size]
    rows = []
    for e in chunk:
        doc_id = str(e.get("id") or e.get("doc_id") or "")
        name = str(e.get("title") or e.get("name") or doc_id)
        locked = "🔒" if e.get("password") or e.get("locked") else ""
        on = bool(e.get("enabled") or e.get("on"))
        toggle = "ON" if on else "OFF"
        rows.append([InlineKeyboardButton(f"{toggle} · {locked}{name}", callback_data=f"kb:toggle:{doc_id}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"kb:page:{page-1}"))
    nav.append(InlineKeyboardButton("🔁 Обновить", callback_data=f"kb:page:{page}"))
    if is_admin:
        nav.append(InlineKeyboardButton("🗂 Синхронизировать", callback_data="kb:sync:_"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)

def _is_admin(user_id) -> bool:
    try:
        f = getattr(_db_mod, "is_admin", None) or getattr(_db_mod, "user_is_admin", None)
        if f:
            return bool(f(user_id))
    except Exception:
        pass
    admin_ids = os.getenv("ADMIN_IDS", "")
    return str(user_id) in {s.strip() for s in admin_ids.split(",") if s.strip()}

async def _db_list_dialogs(chat_id) -> List[Dict[str, Any]]:
    for fn in ("list_dialogs", "get_dialogs", "load_dialogs", "list_user_dialogs"):
        f = getattr(_db_mod, fn, None) if _db_mod else None
        if f:
            try:
                return await f(chat_id) if asyncio.iscoroutinefunction(f) else f(chat_id)
            except Exception:
                pass
    return []

async def _db_set_current_dialog(chat_id, dialog_id: str) -> bool:
    for fn in ("set_current_dialog", "activate_dialog", "use_dialog"):
        f = getattr(_db_mod, fn, None) if _db_mod else None
        if f:
            try:
                res = await f(chat_id, dialog_id) if asyncio.iscoroutinefunction(f) else f(chat_id, dialog_id)
                return True if (res is None or res is True) else False
            except Exception:
                pass
    return True

async def _db_create_dialog(chat_id, title: str):
    for fn in ("create_dialog", "new_dialog", "create_new_dialog"):
        f = getattr(_db_mod, fn, None) if _db_mod else None
        if f:
            try:
                return await f(chat_id, title) if asyncio.iscoroutinefunction(f) else f(chat_id, title)
            except Exception:
                pass
    return {"id": str(int(time.time()*1000)), "title": title}

async def _db_reset_dialog(chat_id) -> bool:
    for fn in ("reset_dialog", "clear_dialog", "drop_dialog", "reset_history"):
        f = getattr(_db_mod, fn, None) if _db_mod else None
        if f:
            try:
                res = await f(chat_id) if asyncio.iscoroutinefunction(f) else f(chat_id)
                return True if (res is None or res is True) else False
            except Exception:
                pass
    return False

async def _kb_list_entries(chat_id, did) -> List[Dict[str, Any]]:
    for fn in ("list_kb_docs", "list_documents", "get_kb_docs", "kb_list"):
        f = getattr(_db_mod, fn, None) if _db_mod else None
        if f:
            try:
                res = await f(chat_id, did) if asyncio.iscoroutinefunction(f) else f(chat_id, did)
                if isinstance(res, list):
                    return res
            except Exception:
                pass
    return []

async def _kb_toggle(chat_id, did, doc_id, turn_on: Optional[bool]=None) -> bool:
    for fn in ("attach_doc", "kb_on", "enable_doc"):
        f = getattr(_db_mod, fn, None) if _db_mod else None
        if f and (turn_on is True or turn_on is None):
            try:
                await f(chat_id, did, doc_id) if asyncio.iscoroutinefunction(f) else f(chat_id, did, doc_id)
                return True
            except Exception:
                pass
    for fn in ("detach_doc", "kb_off", "disable_doc"):
        f = getattr(_db_mod, fn, None) if _db_mod else None
        if f and (turn_on is False or turn_on is None):
            try:
                await f(chat_id, did, doc_id) if asyncio.iscoroutinefunction(f) else f(chat_id, did, doc_id)
                return True
            except Exception:
                pass
    return False

async def _call_user_search(query: str) -> List[Dict[str, str]]:
    for mod_name in ("bot.web_search", "web_search", "bot.web", "web"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for fn_name in ("search", "web_search", "lookup"):
            fn = getattr(mod, fn_name, None)
            if fn:
                try:
                    res = await fn(query) if asyncio.iscoroutinefunction(fn) else fn(query)
                    if isinstance(res, list):
                        return res
                except Exception:
                    pass
    return []

# Commands
async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Здоров! Я помогу искать ответы в документах из БЗ и вести диалоги в разных стилях.\n"
        "Все команды тут — /help"
    )

async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Команды:\n"
        "/start — приветствие\n"
        "/help — подсказка\n"
        "/dialogs — список диалогов\n"
        "/dialog <id> — переключить диалог\n"
        "/reset — очистить текущий диалог\n"
        "/kb — панель базы знаний\n"
        "/style [имя] — установить стиль\n"
        "/model [имя] — установить модель\n"
        "/stats — статистика\n"
        "/img <описание> — сгенерировать изображение\n"
        "/search <запрос> — найти в интернете\n"
    )

async def _img_impl(prompt: str) -> Optional[str]:
    if not prompt: return None
    for fn_name in ("image_generate", "images_generate", "create_image", "image"):
        fn = getattr(_openai_helper, fn_name, None) if _openai_helper else None
        if fn:
            try:
                res = await fn(prompt, size="1024x1024", n=1)
                if isinstance(res, (list, tuple)) and res:
                    return str(res[0])
                if isinstance(res, dict):
                    data = res.get("data") or []
                    if data and isinstance(data, list):
                        item = data[0]
                        if isinstance(item, dict):
                            if "url" in item:
                                return str(item["url"])
                            if "b64_json" in item:
                                return "B64:" + item["b64_json"]
                return str(res)
            except Exception:
                return None
    return None

async def _cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    prompt = " ".join(args).strip()
    if not prompt:
        await update.effective_message.reply_text("Формат: /img описание картинки")
        return
    try:
        await update.effective_chat.send_chat_action("upload_photo")
    except Exception:
        pass
    url_or_b64 = await _img_impl(prompt)
    if not url_or_b64:
        await update.effective_message.reply_text("Не удалось сгенерировать изображение (проверьте ключ/доступы к OpenAI).")
        return
    if url_or_b64.startswith("B64:"):
        import base64, io
        raw = base64.b64decode(url_or_b64[4:])
        bio = io.BytesIO(raw); bio.name = "image.png"
        await update.effective_message.reply_photo(bio, caption="Готово")
    else:
        await update.effective_message.reply_photo(url_or_b64, caption="Готово")

async def _cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = " ".join(context.args or []).strip()
    if not q:
        await update.effective_message.reply_text("Формат: /search запрос")
        return
    results = await _call_user_search(q)
    if not results:
        await update.effective_message.reply_text("Поиск недоступен или ничего не найдено. Проверьте модуль веб-поиска.")
        return
    lines = []
    for i, r in enumerate(results[:5], 1):
        title = r.get("title") or "Без названия"
        url = r.get("url") or ""
        snippet = r.get("snippet") or ""
        lines.append(f"{i}. {title}\n{snippet}\n{url}")
    await update.effective_message.reply_text("Результаты поиска:\n\n" + "\n\n".join(lines))

async def _cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    items = await _db_list_dialogs(chat_id)
    if not items:
        await update.effective_message.reply_text("Диалогов пока нет. Нажмите «Новый», чтобы создать.", reply_markup=_dialogs_keyboard([], 0))
        return
    await update.effective_message.reply_text("Доступные диалоги:", reply_markup=_dialogs_keyboard(items, 0))

async def _cmd_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not context.args:
        await update.effective_message.reply_text("Формат: /dialog <id>")
        return
    did = context.args[0]
    ok = await _db_set_current_dialog(chat_id, did)
    if ok:
        await update.effective_message.reply_text(f"Переключился на диалог: {did}")
        context.chat_data["current_dialog_id"] = did
    else:
        await update.effective_message.reply_text("Диалог не найден или не удалось переключиться.")

async def _db_reset_dialog(chat_id) -> bool:
    for fn in ("reset_dialog", "clear_dialog", "drop_dialog", "reset_history"):
        f = getattr(_db_mod, fn, None) if _db_mod else None
        if f:
            try:
                res = await f(chat_id) if asyncio.iscoroutinefunction(f) else f(chat_id)
                return True if (res is None or res is True) else False
            except Exception:
                pass
    return False

async def _cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    ok = await _db_reset_dialog(chat_id)
    context.chat_data.clear()
    await update.effective_message.reply_text("Диалог очищен." if ok else "Диалог (оперативно) очищен. (Персист БД не найден)")

async def _cmd_kb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    did = context.chat_data.get("current_dialog_id")
    if not did:
        await update.effective_message.reply_text("Нет активного диалога. Сначала выберите диалог через /dialogs или /dialog <id>.")
        return
    entries = await _kb_list_entries(chat_id, did)
    admin = _is_admin(update.effective_user.id if update.effective_user else None)
    await update.effective_message.reply_text("База знаний диалога:", reply_markup=_kb_keyboard(entries, 0, is_admin=admin))

async def _cmd_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    styles = ["default", "mcwilliams", "expert", "short", "thesis", "strictRAG", "noRAG"]
    if not context.args:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(s, callback_data=f"style:set:{s}")] for s in styles])
        await update.effective_message.reply_text("Выберите стиль:", reply_markup=kb)
        return
    chosen = context.args[0].strip()
    if chosen not in styles:
        await update.effective_message.reply_text("Стиль не найден. Доступные: " + ", ".join(styles))
        return
    did = context.chat_data.get("current_dialog_id")
    for fn in ("set_style", "set_dialog_style"):
        f = getattr(_db_mod, fn, None) if _db_mod else None
        if f and did:
            try:
                await f(did, chosen) if asyncio.iscoroutinefunction(f) else f(did, chosen)
                break
            except Exception:
                pass
    context.chat_data["style"] = chosen
    await update.effective_message.reply_text(f"Стиль установлен: {chosen}")

async def _cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    models = []
    env_models = os.getenv("ALLOWED_MODELS", "")
    if env_models:
        models = [m.strip() for m in env_models.split(",") if m.strip()]
    if not models and _openai_helper and hasattr(_openai_helper, "available_models"):
        try:
            models = _openai_helper.available_models()
        except Exception:
            pass
    if not models:
        models = ["gpt-4o", "o4-mini", "gpt-4o-mini"]
    if not context.args:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(m, callback_data=f"model:set:{m}")] for m in models])
        await update.effective_message.reply_text("Выберите модель:", reply_markup=kb)
        return
    chosen = context.args[0].strip()
    if chosen not in models:
        await update.effective_message.reply_text("Модель недоступна. Доступные: " + ", ".join(models))
        return
    did = context.chat_data.get("current_dialog_id")
    for fn in ("set_model", "set_dialog_model"):
        f = getattr(_db_mod, fn, None) if _db_mod else None
        if f and did:
            try:
                await f(did, chosen) if asyncio.iscoroutinefunction(f) else f(did, chosen)
                break
            except Exception:
                pass
    context.chat_data["model"] = chosen
    await update.effective_message.reply_text(f"Модель установлена: {chosen}")

async def _cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    did = context.chat_data.get("current_dialog_id")
    for fn in ("get_stats", "dialog_stats", "kb_stats"):
        f = getattr(_db_mod, fn, None) if _db_mod else None
        if f and did:
            try:
                data = await f(chat_id, did) if asyncio.iscoroutinefunction(f) else f(chat_id, did)
                if isinstance(data, dict):
                    lines = [f"- {k}: {v}" for k, v in data.items()]
                    await update.effective_message.reply_text("Статистика:\n" + "\n".join(lines))
                    return
            except Exception:
                pass
    entries = await _kb_list_entries(chat_id, did) if did else []
    total_docs = len(entries)
    total_chunks = sum(int(e.get("chunks", 0) or 0) for e in entries)
    locked = sum(1 for e in entries if e.get("password") or e.get("locked"))
    lines = [
        f"Диалог: {did or 'нет'}",
        f"Стиль: {context.chat_data.get('style', 'default')}",
        f"Модель: {context.chat_data.get('model', 'default')}",
        f"Документов в БЗ: {total_docs} (🔒: {locked})",
        f"Суммарно чанков: {total_chunks}",
    ]
    await update.effective_message.reply_text("Статистика (сводка):\n" + "\n".join(lines))

# Callbacks
async def _cb_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query; await q.answer()
    data = q.data or ""
    m = re.match(r"^dlg:(\w+):(.+)$", data)
    if not m: return
    action, payload = m.group(1), m.group(2)
    chat_id = update.effective_chat.id if update.effective_chat else None

    if action == "page":
        page = int(payload) if payload.isdigit() else 0
        items = await _db_list_dialogs(chat_id)
        await q.edit_message_reply_markup(reply_markup=_dialogs_keyboard(items, page)); return
    if action == "sel":
        did = payload
        ok = await _db_set_current_dialog(chat_id, did)
        if ok:
            context.chat_data["current_dialog_id"] = did
            await q.edit_message_text(f"Текущий диалог: {did}")
        else:
            await q.answer("Не удалось переключиться", show_alert=True)
        return
    if action == "new":
        res = await _db_create_dialog(chat_id, time.strftime("%Y-%m-%d %H:%M"))
        did = str(res["id"]) if isinstance(res, dict) else str(res)
        await _db_set_current_dialog(chat_id, did)
        context.chat_data["current_dialog_id"] = did
        await q.edit_message_text(f"Создан новый диалог: {did}")
        return
    if action == "rename":
        await q.answer("Переименование добавим в следующем патче (/dialog rename ...).", show_alert=True); return
    if action == "delete":
        did = context.chat_data.get("current_dialog_id")
        if not did:
            await q.answer("Нет активного диалога", show_alert=True); return
        # try delete if function available
        f = getattr(_db_mod, "delete_dialog", None)
        if f:
            try:
                await f(chat_id, did) if asyncio.iscoroutinefunction(f) else f(chat_id, did)
            except Exception:
                pass
        context.chat_data.pop("current_dialog_id", None)
        await q.edit_message_text("Диалог удалён."); return

async def _cb_kb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query; await q.answer()
    data = q.data or ""
    m = re.match(r"^kb:(\w+):(.+)$", data)
    if not m: return
    action, payload = m.group(1), m.group(2)
    chat_id = update.effective_chat.id if update.effective_chat else None
    did = context.chat_data.get("current_dialog_id")
    if action == "page":
        page = int(payload) if payload.isdigit() else 0
        entries = await _kb_list_entries(chat_id, did)
        await q.edit_message_reply_markup(reply_markup=_kb_keyboard(entries, page, _is_admin(update.effective_user.id))); return
    if action == "toggle":
        doc_id = payload
        await _kb_toggle(chat_id, did, doc_id, turn_on=None)
        entries = await _kb_list_entries(chat_id, did)
        await q.edit_message_reply_markup(reply_markup=_kb_keyboard(entries, 0, _is_admin(update.effective_user.id))); return
    if action == "sync":
        if not _is_admin(update.effective_user.id):
            await q.answer("Недостаточно прав", show_alert=True); return
        for fn in ("kb_sync", "sync_kb", "sync"):
            f = getattr(_db_mod, fn, None) if _db_mod else None
            if f:
                try:
                    await f() if asyncio.iscoroutinefunction(f) else f()
                    break
                except Exception:
                    pass
        await q.edit_message_text("Синхронизация завершена. Обновите список."); return

# Voice echo + shortcuts
VOICE_DRAW_PAT = r"^(?:нарисуй|сгенерируй(?:\s+картинку)?|сделай(?:\s+изображение)?)\s+(.+)$"
VOICE_SEARCH_PAT = r"^(?:найди(?:\s+в\s+интернете)?|поищи|погугли)\s+(.+)$"

async def _voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    voice = update.effective_message.voice if update.effective_message else None
    if not voice:
        return
    file_id = voice.file_id
    recognized = None
    try:
        tg_file = await context.bot.get_file(file_id)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "voice.oga")
            await tg_file.download_to_drive(path)
            for fn_name in ("transcribe", "transcribe_audio", "audio_to_text", "speech_to_text", "whisper", "audio_transcribe"):
                fn = getattr(_openai_helper, fn_name, None) if _openai_helper else None
                if fn:
                    try:
                        recognized = await fn(path) if asyncio.iscoroutinefunction(fn) else fn(path)
                        if recognized:
                            break
                    except Exception:
                        continue
    except Exception:
        pass

    if not recognized:
        return

    recognized = str(recognized).strip()
    if not recognized:
        return

    await update.effective_message.reply_text(f"🎙️ Вы сказали: {recognized}", disable_notification=True)

    m = re.match(VOICE_DRAW_PAT, recognized, flags=re.IGNORECASE)
    if m:
        prompt = m.group(1).strip()
        context.args = prompt.split()
        await _cmd_img(update, context)
        raise ApplicationHandlerStop()

    m = re.match(VOICE_SEARCH_PAT, recognized, flags=re.IGNORECASE)
    if m:
        q = m.group(1).strip()
        context.args = [q]
        await _cmd_search(update, context)
        raise ApplicationHandlerStop()

# Unknown + install
async def _cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""
    await update.effective_message.reply_text(
        f"Неизвестная команда: {text}\nДоступные: " + ", ".join(sorted(f'/{c}' for c in _known_commands))
    )

def _has_command(app, name: str) -> bool:
    try:
        handlers_groups = getattr(app, "_handlers", {})
        for grp, lst in handlers_groups.items():
            for h in lst:
                cmds = getattr(h, "commands", None)
                if cmds and name in {c if isinstance(c, str) else str(c) for c in cmds}:
                    return True
    except Exception:
        pass
    return False

async def _set_commands(bot):
    try:
        cmds = [BotCommand(c, desc) for c, desc in [
            ("start", "начать"),
            ("help", "помощь"),
            ("dialogs", "список диалогов"),
            ("dialog", "переключить диалог"),
            ("reset", "очистить диалог"),
            ("kb", "база знаний диалога"),
            ("style", "стиль ответов"),
            ("model", "модель"),
            ("stats", "статистика"),
            ("img", "изображение"),
            ("search", "веб-поиск"),
        ]]
        await bot.set_my_commands(cmds)
    except Exception as e:
        logger.warning("set_my_commands failed: %s", e)

def _install_handlers(app: "Application"):
    if getattr(app, "_hotfix_handlers_installed_v4", False):
        return

    if not _has_command(app, "start"):
        app.add_handler(CommandHandler("start", _cmd_start), group=0)
    if not _has_command(app, "help"):
        app.add_handler(CommandHandler("help", _cmd_help), group=0)
    if not _has_command(app, "dialogs"):
        app.add_handler(CommandHandler("dialogs", _cmd_dialogs), group=0)
    if not _has_command(app, "dialog"):
        app.add_handler(CommandHandler("dialog", _cmd_dialog), group=0)
    if not _has_command(app, "reset"):
        app.add_handler(CommandHandler("reset", _cmd_reset), group=0)
    if not _has_command(app, "kb"):
        app.add_handler(CommandHandler("kb", _cmd_kb), group=0)
    if not _has_command(app, "style"):
        app.add_handler(CommandHandler("style", _cmd_style), group=0)
    if not _has_command(app, "model"):
        app.add_handler(CommandHandler("model", _cmd_model), group=0)
    if not _has_command(app, "stats"):
        app.add_handler(CommandHandler("stats", _cmd_stats), group=0)
    if not _has_command(app, "img"):
        app.add_handler(CommandHandler("img", _cmd_img), group=0)
    if not _has_command(app, "search"):
        app.add_handler(CommandHandler("search", _cmd_search), group=0)

    app.add_handler(CallbackQueryHandler(_cb_dialogs, pattern=r"^dlg:"), group=0)
    app.add_handler(CallbackQueryHandler(_cb_kb, pattern=r"^kb:"), group=0)

    app.add_handler(MessageHandler(filters.VOICE, _voice_handler), group=-1)

    unknown_filter = filters.COMMAND & ~filters.Command(list(_known_commands))
    app.add_handler(MessageHandler(unknown_filter, _cmd_unknown), group=1)

    old_post_init = getattr(app, "post_init", None)
    async def _post_init(new_app: "Application"):
        if old_post_init:
            await old_post_init(new_app)
        await _set_commands(new_app.bot)
    setattr(app, "post_init", _post_init)

    setattr(app, "_hotfix_handlers_installed_v4", True)
    logger.info("Handlers installed (v4).")

if ApplicationBuilder is not None:
    _orig_build = ApplicationBuilder.build
    def _patched_build(self, *a, **kw):
        app = _orig_build(self, *a, **kw)
        try:
            _install_handlers(app)
        except Exception as e:
            logger.exception("Failed to install handlers (v4): %s", e)
        return app
    ApplicationBuilder.build = _patched_build
    logger.info("ApplicationBuilder.build patched (v4) to auto-install command handlers.")
