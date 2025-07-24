import logging
from telegram import BotCommand
from telegram.ext import ApplicationBuilder
from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper
from bot.plugin_manager import PluginManager
from bot.error_tracer import init_error_tracer
from bot.knowledge_base.retriever import Retriever
from bot.knowledge_base.context_manager import ContextManager
def setup_logging(level): logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
async def _post_init(app, bot, settings):
    cmds=[BotCommand("help","помощь"),BotCommand("kb","база знаний"),BotCommand("kb_sync","синхронизация БЗ (админ)"),BotCommand("kb_reset","сброс контекста"),BotCommand("list_models","модели"),BotCommand("set_model","выбрать модель"),BotCommand("image","сгенерировать изображение")]
    await app.bot.set_my_commands(cmds); await bot.post_init(app) if hasattr(bot,'post_init') else None
def main():
    st=load_settings(); setup_logging(st.log_level); init_error_tracer(st.sentry_dsn)
    openai_config={"api_key":st.openai_api_key,"model":st.openai_model,"vision_model":st.vision_model,"image_model":st.image_model,"image_size":"1024x1024","temperature":st.openai_temperature,"max_tokens":st.max_tokens,"assistant_prompt":"You are a helpful assistant.","max_history_size":st.max_history_size,"vision_max_tokens":st.vision_max_tokens,"allowed_models_whitelist":st.allowed_models_whitelist,"denylist_models":st.denylist_models}
    openai_helper=OpenAIHelper(openai_config, PluginManager({}))
    retriever=Retriever(top_k=st.rag_top_k); ctx=ContextManager()
    bot=ChatGPTTelegramBot(config={"token":st.telegram_bot_token,"enable_image_generation":st.enable_image_generation}, openai_helper=openai_helper, retriever=retriever, ctx_manager=ctx)
    async def post_init(app): await _post_init(app, bot, st)
    app=ApplicationBuilder().token(st.telegram_bot_token).post_init(post_init).build()
    bot.register_handlers(app); app.run_polling()
if __name__=="__main__": main()
