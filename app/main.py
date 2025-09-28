
from telegram.ext import Application
from .settings import load_settings
from .logging_config import setup_logging
from .bootstrap import build
from .router import register
from . import lifecycle

def run():
    setup_logging()
    settings = load_settings()
    app = Application.builder().token(settings.telegram_token).build()
    app.bot_data.update(build(settings))
    register(app)
    app.post_init.append(lifecycle.on_startup)
    app.post_shutdown.append(lifecycle.on_shutdown)
    app.run_polling(close_loop=False, allowed_updates=None)
