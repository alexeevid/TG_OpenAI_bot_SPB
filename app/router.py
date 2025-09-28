
from telegram.ext import Application
from .handlers import start, dialogs, kb, model, mode, img, web, stats, admin, text, voice

def register(app: Application) -> None:
    start.register(app)
    dialogs.register(app)
    kb.register(app)
    model.register(app)
    mode.register(app)
    img.register(app)
    web.register(app)
    stats.register(app)
    admin.register(app)
    voice.register(app)
    text.register(app)
