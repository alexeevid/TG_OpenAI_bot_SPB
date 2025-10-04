from telegram.ext import Application
from .settings import load_settings
from .logging_config import setup_logging
from .bootstrap import build
from .router import register
from . import lifecycle

def run():
    setup_logging()
    settings = load_settings()

    builder = (
        Application.builder()
        .token(settings.telegram_token)
        .post_init(lifecycle.on_startup)
        .post_shutdown(lifecycle.on_shutdown)
    )
    app = builder.build()

    app.bot_data.update(build(settings))
    register(app)

    # Важно: drop_pending_updates=True — дополнительно чистим хвост,
    # чтобы исключить конфликты и зависшие апдейты.
    app.run_polling(
        close_loop=False,
        allowed_updates=None,
        drop_pending_updates=True,
    )
