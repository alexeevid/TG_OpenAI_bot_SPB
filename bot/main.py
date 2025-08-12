# bot/main.py
from bot.telegram_bot import build_app
from telegram import Update

def main():
    app = build_app()
    # run_polling сам создаёт/закрывает цикл, post_init выполнится перед стартом
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
