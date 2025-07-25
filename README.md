
# Telegram OpenAI RAG Bot (Yandex Disk, no pgvector)

Готовый бот с:
- RAG поверх Яндекс.Диска (WebDAV): `/kb_sync`, `/kb`, `/kb_search`
- Выбором документов в контекст (inline кнопки)
- Косинусный поиск по эмбеддингам без pgvector (храним в ARRAY(Float))
- Голос → текст → мгновенный ответ
- Генерация изображений (`/image`) с `dall-e-3`
- Вайтлист пользователей (`ALLOWED_USER_IDS`), админ-команды (`ADMIN_USER_IDS`)

## Быстрый старт

1. Залей этот проект в GitHub / Railway.
2. Проставь переменные окружения (см. `.env.example`):
   - `DB_URL` вида `postgresql+psycopg://...`
   - `YANDEX_DISK_TOKEN`, `YANDEX_ROOT_PATH`
   - `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`
3. Задеплой (Dockerfile уже есть).

В логах должно быть:
```
Tables created via Base.metadata.create_all()
Application started
```

## Команды

- `/help` — помощь
- `/reset` — сброс диалога
- `/kb` — показать документы БЗ (из БД) + выбрать в контекст
- `/kb_reset` — сброс выбранных документов
- `/kb_search <вопрос>` — задать вопрос с учётом выбранных документов
- `/kb_sync` — синхронизация БЗ с Я.Диска (только админам)
- `/pdfpass <filename.pdf> <password>` — сохранить пароль для PDF перед `/kb_sync`
- `/image <prompt>` — сгенерировать изображение

## Как это работает

1. `/kb_sync` вытягивает всё с Я.Диска (WebDAV), парсит, режет на чанки, эмбеддит, складывает в Postgres.
2. `/kb` — показывает список документов (из Postgres), позволяя выбрать нужные в контекст.
3. `/kb_search` — эмбеддит запрос, ищет ближайшие чанки в выбранных документах, отправляет их + вопрос в LLM.
4. `/reset` сбрасывает историю диалога и выбранный контекст (через `/kb_reset` — только выбор БЗ).

## Важно

- Используется `httpx==0.26.0`, чтобы не конфликтовать с `python-telegram-bot==20.8`.
- Pydantic v2 + `pydantic-settings`.
- Без Alembic и pgvector — всё работает на `Base.metadata.create_all` и ARRAY(Float).

Удачи! 🚀
