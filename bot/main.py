import logging
import os
from pathlib import Path

from telegram.ext import ApplicationBuilder
from telegram import BotCommand

from bot.config import load_settings
from bot.error_tracer import init_error_tracer
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper
from bot.plugin_manager import PluginManager
from bot.usage_tracker import UsageTracker
from bot.knowledge_base.retriever import Retriever
from bot.knowledge_base.context_manager import ContextManager

# DB & migrations
from bot.db.session import DB_URL, engine
from bot.db.models import Base
from alembic.config import Config
from alembic import command


def setup_logging(level: str):
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )


async def post_init(application, bot: ChatGPTTelegramBot, settings):
    commands = [
        BotCommand("start", "помощь"),
        BotCommand("help", "помощь"),
        BotCommand("reset", "сброс диалога"),
        BotCommand("kb", "база знаний / поиск"),
        BotCommand("kb_reset", "сброс выбранных документов"),
        BotCommand("kb_sync", "синхронизация базы знаний (админ)"),
        BotCommand("pdfpass", "пароль к PDF"),
        BotCommand("list_models", "показать модели"),
        BotCommand("set_model", "выбрать модель"),
    ]
    if settings.enable_image_generation:
        commands.append(BotCommand("image", "сгенерировать изображение"))
    await application.bot.set_my_commands(commands)


def run_migrations():
    logging.info("DB_URL resolved to: %s", DB_URL)
    try:
        # alembic.ini лежит в корне проекта
        alembic_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
        cfg = Config(str(alembic_ini))
        cfg.set_main_option("sqlalchemy.url", DB_URL)
        command.upgrade(cfg, "head")
        logging.info("Alembic migrations applied successfully")
    except Exception as e:
        logging.error(
            "Can't run alembic migrations, fallback to Base.metadata.create_all: %s",
            e,
            exc_info=True
        )
        Base.metadata.create_all(engine)


def main():
    # 1) Конфиги/логи/трейсинг
    settings = load_settings()
    setup_logging(settings.log_level)
    init_error_tracer(settings.sentry_dsn)

    # 2) Миграции ДОЛЖНЫ пройти до того, как мы создадим объекты, которые могут полезть в БД
    run_migrations()

    # 3) Инициализация OpenAI, RAG, UsageTracker, Bot
    plugin_manager = PluginManager(config={})
    openai_config = {
        "api_key": settings.openai_api_key,
        "model": settings.openai_model,
        "vision_model": settings.vision_model,
        "image_model": settings.image_model,
        "image_size": "1024x1024",
        "tts_model": settings.tts_model,
        "tts_voice": "alloy",
        "temperature": settings.openai_temperature,
        "n_choices": 1,
        "max_tokens": settings.max_tokens,
        "presence_penalty": 0,
        "frequency_penalty": 0,
        "assistant_prompt": "You are a helpful assistant.",
        "max_history_size": settings.max_history_size,
        "max_conversation_age_minutes": 60,
        "show_usage": True,
        "enable_vision_follow_up_questions": False,
        "vision_max_tokens": settings.vision_max_tokens,
        "vision_prompt": "Опиши, что на изображении.",
        "vision_detail": settings.vision_detail,
        "whisper_prompt": "",
        "bot_language": settings.bot_language,
        "proxy": None,
        "enable_image_generation": settings.enable_image_generation,
        "enable_tts_generation": settings.enable_tts_generation,
        "functions_max_consecutive_calls": settings.functions_max_consecutive_calls,
        "allowed_models_whitelist": settings.allowed_models_whitelist,
        "denylist_models": settings.denylist_models,
    }

    openai_helper = OpenAIHelper(config=openai_config, plugin_manager=plugin_manager)
    retriever = Retriever(top_k=settings.rag_top_k)
    ctx_manager = ContextManager()

    bot = ChatGPTTelegramBot(
        config={
            "token": settings.telegram_bot_token,
            "enable_image_generation": settings.enable_image_generation,
            # если используешь whitelist/админов — передай их сюда
            "allowed_user_ids": settings.allowed_user_ids,
            "admin_user_ids": settings.admin_user_ids,
        },
        openai_helper=openai_helper,
        usage_tracker=UsageTracker(),
        retriever=retriever,
        ctx_manager=ctx_manager
    )

    async def _post_init(app):
        await post_init(app, bot, settings)

    # 4) Telegram
    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .build()
    )

    bot.register_handlers(application)

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
