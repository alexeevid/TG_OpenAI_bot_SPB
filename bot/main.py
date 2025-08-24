
import os
from bot.settings import load_settings
from bot.telegram_bot import build_app

def _normalize_domain(val: str) -> str:
    if not val:
        return ""
    val = val.strip()
    if val.startswith("http://") or val.startswith("https://"):
        return val.rstrip("/")
    return f"https://{val.rstrip('/')}"

def main():
    settings = load_settings()
    app = build_app()

    # Railway supplies PORT
    port = int(os.getenv("PORT", "8443"))
    domain = os.getenv("WEBHOOK_DOMAIN") or os.getenv("PUBLIC_URL") or ""
    domain = _normalize_domain(domain)

    token = settings.telegram_bot_token
    url_path = f"webhook/{token}"

    if domain:
        webhook_url = f"{domain}/{url_path}"
        print(f"🔔 Starting webhook on 0.0.0.0:{port} with URL: {webhook_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=url_path,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        print("ℹ️ WEBHOOK_DOMAIN not set — falling back to polling.")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
