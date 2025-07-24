# Установка v2 (Railway)

1. Скачай архив, распакуй, создай repo на GitHub и пушни.
2. Railway:
   - создай Postgres;
   - выстави переменные из `.env.example` (TELEGRAM_BOT_TOKEN, OPENAI_API_KEY и т.д.);
   - в БД: `CREATE EXTENSION IF NOT EXISTS vector;`
   - прогоняй миграции `alembic upgrade head` (можно добавить как prestart шаг);
   - деплой.
3. Телеграм проверь: `/help`, `/kb_sync` (для ADMIN_IDS), `/kb`, `/kb <query>`, `/image`, фото/голос.
