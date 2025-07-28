# Telegram RAG Bot (OpenAI + Yandex.Disk REST)

- PTB v20
- OpenAI API (Responses or Chat Completions)
- PostgreSQL via SQLAlchemy + psycopg2-binary
- Yandex.Disk REST listing (sync via inline button in /kb)

## Commands
/start, /help, /reset, /stats, /kb, /model, /dialogs, /dialog <id>

## ENV (.env)
TELEGRAM_BOT_TOKEN=...
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o-mini
IMAGE_MODEL=gpt-image-1
DATABASE_URL=postgresql://user:pass@host:5432/db
YANDEX_DISK_TOKEN=...
YANDEX_ROOT_PATH=/База Знаний
ADMIN_USER_IDS=12345
ALLOWED_TELEGRAM_USER_IDS=12345,67890
