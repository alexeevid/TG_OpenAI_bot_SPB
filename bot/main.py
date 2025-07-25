from telegram.ext import ApplicationBuilder
from bot.telegram_bot import ChatGPTTelegramBot
from bot.settings import settings
import asyncio


def main():
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    bot = ChatGPTTelegramBot(settings)

    # корректно инициализируем асинхронный метод
    asyncio.run(bot.initialize(app))

    # запускаем бота — не внутри event loop!
    app.run_polling()


if __name__ == "__main__":
    main()
