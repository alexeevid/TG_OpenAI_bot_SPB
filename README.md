# Telegram Bot — OpenAI + RAG (PostgreSQL + PGVector) + Yandex.Disk

Готовая сборка для Railway. Поддерживает:
- Чат с OpenAI (Chat Completions)
- RAG на PostgreSQL + PGVector
- Синхронизацию Базы знаний с Яндекс.Диском
- Мультимодальность: голос (Whisper), изображения (DALL·E)
- Управление доступом (админы/разрешённые)
- Команды: /start, /help, /reset, /stats, /dialogs, /dialog, /kb, /kb_diag, /model, /mode, /img, /web, /grant, /revoke, /whoami

## Быстрый старт (локально)
1. Создайте БД PostgreSQL и установите расширение:
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```
2. Скопируйте `.env.example` в `.env` и заполните значения.
3. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
4. Инициализируйте схему БД (alembic):
   ```bash
   alembic upgrade head
   ```
5. Запустите бота:
   ```bash
   python -m bot.main
   ```

## Railway
- Укажите переменные окружения из `.env.example`
- Команда запуска: `python -m bot.main`

## Лицензия
MIT
