import asyncio
from telegram.ext import ApplicationBuilder
from bot.telegram_bot import ChatGPTTelegramBot
from bot.settings import settings


async def main():
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    bot = ChatGPTTelegramBot(settings)

    await bot.initialize(app)
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await app.updater.idle()


if __name__ == "__main__":
    app.run_polling()

