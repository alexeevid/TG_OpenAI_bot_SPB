from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from sqlalchemy.orm import Session
from ..services.dialog_service import DialogService
from ..db.session import make_session_factory
from ..db.models import Dialog, User

async def cmd_dialog_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ds: DialogService = context.bot_data['svc_dialog']
    d = ds.get_or_create_active(update.effective_user.id)
    await update.message.reply_text(f"Создан новый диалог #{d.id}")

async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Простой листинг из БД
    repo = context.bot_data['repo_dialogs']
    sf = repo.sf  # session factory
    uid = str(update.effective_user.id)
    rows = []
    with sf() as s:  # type: Session
        u = s.query(User).filter_by(tg_id=uid).first()
        if u:
            rows = (s.query(Dialog)
                      .filter(Dialog.user_id == u.id)
                      .order_by(Dialog.id.desc())
                      .limit(20)
                      .all())
    if not rows:
        await update.message.reply_text("Диалоги не найдены. Наберите /dialog_new для создания.")
        return
    text = "Последние диалоги:\n" + "\n".join([f"• #{d.id} — {d.title or 'без названия'}" for d in rows])
    await update.message.reply_text(text)

def register(app: Application) -> None:
    app.add_handler(CommandHandler("dialog_new", cmd_dialog_new))
    app.add_handler(CommandHandler("dialogs", cmd_dialogs))
