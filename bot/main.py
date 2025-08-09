import logging
from bot.settings import load_settings
from bot.telegram_bot import build_app

def main():
    settings = load_settings()
    # ✅ более «шумная» настройка логов
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )
    # немного настроим сторонние логгеры
    logging.getLogger("telegram").setLevel(logging.INFO)    # чтобы не заливало дебагом PTB
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)  # видеть SQL (можно DEBUG)
    logging.getLogger("bot").setLevel(logging.DEBUG)        # наши модули

    app = build_app()
    app.run_polling(allowed_updates=None)

if __name__=='__main__':
    main()
