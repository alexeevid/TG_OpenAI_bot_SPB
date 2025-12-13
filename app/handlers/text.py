from app.services.gen_service import GenService

gen = GenService()

async def on_text(update, context):
    dialog = context.user_data.get("dialog", {})
    history = dialog.get("history", [])
    model = dialog.get("model")
    style = dialog.get("style", "")
    system = style or "You are a helpful assistant."
    answer = gen.chat(update.message.text, history, model, system)
    await update.message.reply_text(answer)
    history.append({"role": "user", "content": update.message.text})
    history.append({"role": "assistant", "content": answer})
    dialog["history"] = history[-20:]
    context.user_data["dialog"] = dialog