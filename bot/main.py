# bot/main.py
import logging
from telegram.ext import Application
from telegram import BotCommand

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper

# ——— Включаем DEBUG-логи для всего приложения ——————————————————
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
)
# ——————————————————————————————————————————————————————————

def build_application() -> Application:
    settings = load_settings()

    # OpenAI helper в вашем формате
    openai = OpenAIHelper(api_key=settings.openai_api_key)

    # Наш класс бота с хендлерами
    bot = ChatGPTTelegramBot(openai=openai, settings=settings)

    async def _post_init(app: Application):
        # 1) Гарантированно гасим вебхук и сбрасываем «зависшие» апдейты
        await app.bot.delete_webhook(drop_pending_updates=True)
        me = await app.bot.get_me()
        logger.info("🤖 Connected as @%s (id=%s)", me.username, me.id)

        # 2) Ставим команды:
        try:
            if hasattr(bot, "setup_commands") and callable(getattr(bot, "setup_commands")):
                # Если в вашем классе вдруг появится метод setup_commands(app)
                await bot.setup_commands(app)
            elif hasattr(bot, "_post_init") and callable(getattr(bot, "_post_init")):
                # Если уже есть внутренний _post_init(app), отдадим ему право всё настроить
                await bot._post_init(app)  # type: ignore[attr-defined]
            elif hasattr(bot, "_apply_bot_commands") and callable(getattr(bot, "_apply_bot_commands")):
                # Старое приватное API —  установит команды во всех scope
                await bot._apply_bot_commands(app.bot, lang=getattr(bot.settings, "bot_language", None))  # type: ignore[attr-defined]
            else:
                # Фолбэк: минимальный набор команд на всякий случай
                commands = [
                    BotCommand("help", "помощь"),
                    BotCommand("reset", "сброс контекста"),
                    BotCommand("stats", "статистика"),
                    BotCommand("kb", "база знаний"),
                    BotCommand("model", "выбор модели"),
                    BotCommand("mode", "стиль ответов"),
                    BotCommand("dialogs", "диалоги"),
                    BotCommand("img", "сгенерировать изображение"),
                    BotCommand("web", "веб-поиск"),
                ]
                await app.bot.set_my_commands(commands=commands)
                logger.info("✅ Команды установлены (fallback)")
        except Exception as e:
            logger.exception("setup_commands failed: %s", e)

    # Сборка Application с post_init
    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
    )
    app = builder.build()

    # Регистрируем все хендлеры
    bot.install(app)

    return app


def main() -> None:
    logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    app = build_application()
    logger.info("🚀 Бот запускается (run_polling)...")

    # ВАЖНО: запускаем единственный инстанс, сбрасываем подвешенные апдейты, разрешаем все типы
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=None,
    )


if __name__ == "__main__":
    main()
