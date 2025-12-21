from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.authz_service import AuthzService
from ..services.rag_service import RagService

async def kb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Использование: /kb <запрос>")
        return
    rag: RagService = context.bot_data.get("svc_rag")
    if not rag:
        await update.message.reply_text("⚠️ База знаний не настроена.")
        return
    results = rag.retrieve(query, dialog_id=0, top_k=5)
    if not results:
        await update.message.reply_text("Ничего не найдено.")
        return
    lines = [f"Найдено фрагментов: {len(results)}"]
    for i, chunk in enumerate(results, start=1):
        text = chunk.text
        if len(text) > 400:
            text = text[:400] + "..."
        lines.append(f"{i}. {text}")
    await update.message.reply_text("\n".join(lines))

def register(app: Application) -> None:
    app.add_handler(CommandHandler("kb", kb_handler))
