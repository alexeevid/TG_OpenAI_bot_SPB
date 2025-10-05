from telegram.ext import Application

# Базовые модули, которые точно есть
from .handlers import start, dialogs, kb, model, mode, img, web, stats, admin, text, voice

# errors импортируем безопасно — чтобы не уронить приложение, если файла нет/ошибка внутри
try:
    from .handlers import errors as errors_mod
except Exception as e:
    errors_mod = None
    import logging
    logging.getLogger(__name__).exception("router: errors module import failed: %s", e)

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
    voice.register(app)   # голос ставим до text
    text.register(app)
    if errors_mod:
        errors_mod.register(app)  # подключаем только если импорт прошёл
