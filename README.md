
# TG OpenAI Modular Bot — v3 (Voice + RAG PGVector)

## Быстрый старт
1) ENV (Railway либо локально):
   - TELEGRAM_BOT_TOKEN
   - OPENAI_API_KEY
   - DATABASE_URL (postgresql://...)
   - Остальные переменные см. ниже
2) Миграции:
   ```bash
   alembic upgrade head
   ```
3) Запуск:
   ```bash
   python run_local.py
   ```

### Переменные окружения (сохранены имена)
- TELEGRAM_BOT_TOKEN
- OPENAI_API_KEY
- DATABASE_URL
- BOT_LANGUAGE, CHUNK_SIZE, CHUNK_OVERLAP, MAX_CONTEXT_TOKENS, MAX_KB_CHUNKS
- ENABLE_IMAGE_GENERATION (1/0), ENABLE_WEB_SEARCH (1/0)
- OPENAI_IMAGE_MODEL, OPENAI_TEXT_MODEL, OPENAI_EMBEDDING_MODEL, OPENAI_TEMPERATURE
- ADMIN_USER_IDS, ALLOWED_USER_IDS
- YANDEX_DISK_TOKEN, YANDEX_ROOT_PATH
- KB_SYNC_INTERVAL, KB_SYNC_ENTRYPOINT
- RATE_LIMIT_PER_MIN
- WEBHOOK_DOMAIN, WEBHOOK_SECRET, WEB_SEARCH_PROVIDER
- OPENAI_TRANSCRIBE_MODEL (по умолчанию whisper-1)
- PGVECTOR_DIM (по умолчанию 3072)

### Что готово
- Голос: OpenAI Audio Transcriptions (Whisper)
- RAG: PGVector, косинусная дистанция
- Изоляция слоёв: handlers → services → clients/db/kb
- Alembic миграции (pgvector + таблицы)

### Что донастроить позже
- Индексация KB из Я.Диска: `app/kb/syncer.py`
- Индексы ivfflat для kb_chunks.embedding (отдельной миграцией)
