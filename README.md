# Telegram OpenAI RAG Bot — v3

## Что нового
- Ограничение доступа по `ALLOWED_USER_IDS`
- Голос → транскрибация + логический ответ
- `/list_models` — выбор модели кликом
- Починен `/image`
- Универсальный Postgres URL resolver (Railway public/internal/…)
- Миграции Alembic + pgvector

## Быстрый старт
1. `.env.example` → `.env`
2. Railway: сервис + Postgres
3. В БД: `CREATE EXTENSION IF NOT EXISTS vector;`
4. `alembic upgrade head`
5. Деплой → `/help`
