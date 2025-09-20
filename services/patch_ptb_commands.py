
"""
services/patch_ptb_commands.py

Auto-adds /img, /dialogs, /reset and a fallback unknown-command handler to an
existing python-telegram-bot v20 Application.

We monkeypatch ApplicationBuilder.build() to inject handlers into the built app.
This keeps your code unmodified while restoring broken commands.
"""
from __future__ import annotations
import asyncio, logging, importlib
from typing import Optional, Iterable, Any

logger = logging.getLogger(__name__)
if not logger.handlers:
    h = logging.StreamHandler()
    fmt = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
    h.setFormatter(fmt)
    logger.addHandler(h)
logger.setLevel(logging.INFO)

try:
    from telegram import Update, BotCommand
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
except Exception as e:
    logger.warning("telegram not available; patch_ptb_commands inactive: %s", e)
    ApplicationBuilder = None  # type: ignore

# attempt to find user's openai helper
_openai_helper = None
for _mod in ("bot.openai_helper", "openai_helper"):
    try:
        _openai_helper = importlib.import_module(_mod)
        break
    except Exception:
        continue

async def _img_impl(prompt: str) -> Optional[str]:
    """
    Returns URL or file_id to send as photo. Prefers user's helper, else None.
    """
    if not prompt:
        return None
    # common helper names: image_generate / images_generate
    for fn_name in ("image_generate", "images_generate", "create_image"):
        fn = getattr(_openai_helper, fn_name, None) if _openai_helper else None
        if fn:
            try:
                res = await fn(prompt, size="1024x1024", n=1)
                # normalize: list[str] or dict with urls
                if isinstance(res, (list, tuple)) and res:
                    return str(res[0])
                if isinstance(res, dict):
                    # try DALL·E-style structure
                    data = res.get("data") or []
                    if data and isinstance(data, list):
                        item = data[0]
                        if isinstance(item, dict):
                            if "url" in item:
                                return str(item["url"])
                            if "b64_json" in item:
                                # return as bytes? Telegram can accept bytes; but we'd need to decode.
                                import base64
                                raw = base64.b64decode(item["b64_json"])
                                # Save to temp and return path-like string (telegram can read bytes directly via BytesIO in handler)
                                # Here, return a special marker; handler deals with bytes.
                                return "B64:" + item["b64_json"]
                # as a last resort, cast to str
                return str(res)
            except Exception as e:
                logger.exception("image generation helper failed: %s", e)
                return None
    return None

async def _cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    prompt = " ".join(args).strip()
    if not prompt:
        await update.effective_message.reply_text("Формат: /img описание картинки")
        return
    await update.effective_chat.send_chat_action("upload_photo")
    url_or_b64 = await _img_impl(prompt)
    if not url_or_b64:
        await update.effective_message.reply_text("Не удалось сгенерировать изображение (проверьте ключ/доступы к OpenAI).")
        return
    if url_or_b64.startswith("B64:"):
        import base64, io
        raw = base64.b64decode(url_or_b64[4:])
        bio = io.BytesIO(raw)
        bio.name = "image.png"
        await update.effective_message.reply_photo(bio, caption="Готово")
    else:
        await update.effective_message.reply_photo(url_or_b64, caption="Готово")

def _try_import(names: Iterable[str]) -> Optional[Any]:
    for name in names:
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None

async def _cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None

    # Try to find a user-defined function to list dialogs
    mod = _try_import(("bot.dialogs", "dialogs", "bot.db", "db"))
    get_list = None
    for fname in ("list_dialogs", "get_dialogs", "load_dialogs", "list_user_dialogs"):
        get_list = getattr(mod, fname, None) if mod else None
        if get_list:
            break
    if get_list:
        try:
            items = await get_list(chat_id) if asyncio.iscoroutinefunction(get_list) else get_list(chat_id)
            if not items:
                await update.effective_message.reply_text("Диалогов пока нет.")
                return
            lines = []
            for i, d in enumerate(items, 1):
                # support both dict and tuple
                if isinstance(d, dict):
                    did = d.get("id") or d.get("dialog_id") or d.get("did") or ""
                    title = d.get("title") or d.get("name") or d.get("topic") or ""
                elif isinstance(d, (list, tuple)) and len(d) >= 2:
                    did, title = d[0], d[1]
                else:
                    did, title = str(d), ""
                lines.append(f"{i}. {title} — /dialog {did}")
            await update.effective_message.reply_text("Доступные диалоги:\n" + "\n".join(lines))
            return
        except Exception as e:
            logger.exception("/dialogs failed: %s", e)

    # Fallback: at least respond
    await update.effective_message.reply_text("Команда подключена, но источник диалогов не найден. Проверьте модуль БД/диалогов.")

async def _cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None

    # Try call user-defined reset function if present
    mod = _try_import(("bot.dialogs", "dialogs", "bot.db", "db"))
    reset_fn = None
    for fname in ("reset_dialog", "clear_dialog", "drop_dialog", "reset_history"):
        reset_fn = getattr(mod, fname, None) if mod else None
        if reset_fn:
            break
    ok = False
    if reset_fn:
        try:
            res = await reset_fn(chat_id) if asyncio.iscoroutinefunction(reset_fn) else reset_fn(chat_id)
            ok = True if (res is None or res is True) else False
        except Exception as e:
            logger.exception("/reset failed: %s", e)
    # Always clear PTB in-memory context to be safe
    try:
        context.chat_data.clear()
        context.user_data.clear()
    except Exception:
        pass
    await update.effective_message.reply_text("Диалог очищен." if ok else "Диалог (оперативно) очищен. (Персист БД не найден)")

async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Команды:\n"
        "/img <описание> — сгенерировать изображение\n"
        "/dialogs — показать список диалогов\n"
        "/reset — очистить текущий диалог\n"
    )

async def _cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""
    await update.effective_message.reply_text(
        f"Неизвестная команда: {text}\nДоступные: /img, /dialogs, /reset, /help"
    )

def _has_command(app, name: str) -> bool:
    # Try to discover existing CommandHandler for 'name' to avoid duplicates
    try:
        handlers_groups = getattr(app, "_handlers", {})
        for grp, lst in handlers_groups.items():
            for h in lst:
                # CommandHandler exposes .commands (set[str]) in PTB v20
                cmds = getattr(h, "commands", None)
                if cmds and name in {c if isinstance(c, str) else str(c) for c in cmds}:
                    return True
    except Exception:
        pass
    return False

async def _set_commands(bot):
    try:
        cmds = [
            BotCommand("help", "помощь"),
            BotCommand("img", "генерация изображения"),
            BotCommand("dialogs", "список диалогов"),
            BotCommand("reset", "очистить диалог"),
        ]
        await bot.set_my_commands(cmds)
    except Exception as e:
        logger.warning("set_my_commands failed: %s", e)

def _install_handlers(app):
    if getattr(app, "_hotfix_handlers_installed", False):
        return
    # Add our handlers only if not already present
    if not _has_command(app, "img"):
        app.add_handler(CommandHandler("img", _cmd_img), group=0)
    if not _has_command(app, "dialogs"):
        app.add_handler(CommandHandler("dialogs", _cmd_dialogs), group=0)
    if not _has_command(app, "reset"):
        app.add_handler(CommandHandler("reset", _cmd_reset), group=0)
    if not _has_command(app, "help"):
        app.add_handler(CommandHandler("help", _cmd_help), group=0)
    # Fallback for any other unknown command so user gets a response
    app.add_handler(MessageHandler(filters.COMMAND, _cmd_unknown), group=1)

    # Try to publish command list to Telegram menu
    try:
        if hasattr(app, "create_task"):
            app.create_task(_set_commands(app.bot))
        else:
            asyncio.create_task(_set_commands(app.bot))
    except Exception as e:
        logger.warning("Failed to schedule set_my_commands: %s", e)

    setattr(app, "_hotfix_handlers_installed", True)
    logger.info("Handlers installed: /img, /dialogs, /reset, /help + unknown fallback")

# Monkeypatch ApplicationBuilder.build to inject on app creation
if ApplicationBuilder is not None:
    _orig_build = ApplicationBuilder.build

    def _patched_build(self, *a, **kw):
        app = _orig_build(self, *a, **kw)
        try:
            _install_handlers(app)
        except Exception as e:
            logger.exception("Failed to install handlers: %s", e)
        return app

    ApplicationBuilder.build = _patched_build
    logger.info("ApplicationBuilder.build patched to auto-install command handlers.")
