from telegram.ext import ApplicationBuilder
from bot.telegram_bot import ChatGPTTelegramBot
from bot.settings import settings
import asyncio


def main():
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    bot = ChatGPTTelegramBot(settings)

    async def start():
        await bot.initialize(app)
        app.run_polling()

    asyncio.run(start())


if __name__ == "__main__":
    main()
