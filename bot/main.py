import logging
from bot.settings import load_settings
from bot.telegram_bot import build_app

def main():
    settings=load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    app=build_app(); app.run_polling(allowed_updates=None)

if __name__=='__main__':
    main()
