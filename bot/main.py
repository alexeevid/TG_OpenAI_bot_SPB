import os
from bot.telegram_bot import app
from bot.settings import load_settings
from bot.telegram_bot import build_app
from telegram import Update
# ...
app.run_webhook(
    listen="0.0.0.0",
    port=port,
    url_path=url_path,           # "/webhook"
    webhook_url=webhook_url,
    allowed_updates=Update.ALL_TYPES,  # <—
    drop_pending_updates=True,
)

def _normalize_domain(val: str) -> str:
    if not val:
        return ""
    val = val.strip()
    if val.startswith(("http://", "https://")):
        return val.rstrip("/")
    return f"https://{val.rstrip('/')}"

def main():
    settings = load_settings()
    app = build_app()

    port = int(os.getenv("PORT", "8443"))
    domain = _normalize_domain(os.getenv("WEBHOOK_DOMAIN") or os.getenv("PUBLIC_URL") or "")
    secret = os.getenv("WEBHOOK_SECRET", "")

    url_path = "webhook"  # без токена в URL!
    if domain:
        webhook_url = f"{domain}/{url_path}"
        print(f"🔔 Starting webhook on 0.0.0.0:{port} with URL: {webhook_url}")
        try:
            app.run_webhook(
                listen="0.0.0.0",
                port=port,
                url_path=url_path,
                webhook_url=webhook_url,
                secret_token=secret or None,
                drop_pending_updates=True,
            )
        except RuntimeError as e:
            # если вдруг webhooks-extras не подтянулись — мягко уходим в polling
            if "webhooks" in str(e).lower():
                print("⚠️ PTB без extras для webhooks — fallback на polling.")
                app.run_polling(drop_pending_updates=True)
            else:
                raise
    else:
        print("ℹ️ WEBHOOK_DOMAIN не задан — работаю в режиме polling.")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
