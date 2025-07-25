
# Telegram RAG Bot (OpenAI + Yandex.Disk REST)

Готовый минимальный бот на **python-telegram-bot v20**, OpenAI API и **REST API Яндекс.Диска** (без WebDAV).
- Выбор и подключение документов в контекст прямо из Телеграма
- Генерация изображений (если у организации есть доступ к `gpt-image-1`)
- Ограничение доступа к боту по списку user_id
- PostgreSQL / SQLite для хранения списка документов

## 1. Быстрый старт

```bash
git clone <ваш репозиторий>
cd repo
cp .env.example .env
# заполните .env своими значениями
docker build -t tg-bot .
docker run --env-file .env tg-bot
```

Или задеплойте на Railway / Fly.io / Render.

## 2. Переменные окружения

Смотрите `.env.example`. Обязательные:

```
TELEGRAM_BOT_TOKEN=...
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o-mini
IMAGE_MODEL=gpt-image-1   # или отключите /image, изменив команду в telegram_bot.py
YANDEX_DISK_TOKEN=...     # токен с правом cloud_api:disk.read (REST)
YANDEX_ROOT_PATH=/База Знаний
DATABASE_URL=postgresql+psycopg://user:pass@host:port/db
ADMIN_USER_IDS=123456
ALLOWED_TELEGRAM_USER_IDS=123456,987654
```

## 3. Команды

- `/start`, `/help` — помощь  
- `/reset` — сброс контекста (в этой версии история не хранится, просто placeholder)  
- `/kb` — показать файлы из БД, выбрать те, что войдут в контекст  
- `/kb_search <вопрос>` — спросить с учетом выбранных файлов  
- `/kb_reset` — сбросить выбранные файлы  
- `/kb_sync` — синхронизировать Яндекс.Диск -> БД (только админ)  
- `/image <prompt>` — сгенерировать изображение (OpenAI)

## 4. Синхронизация с Яндекс.Диском (REST)

Мы используем **REST API** (не WebDAV). Нужен OAuth-токен с *cloud_api:disk.read*.  
Команда `/kb_sync` обходит `YANDEX_ROOT_PATH`, собирает все файлы в БД.  
Затем `/kb` выводит список (первые 30) и дает кнопки для выбора.

## 5. Миграции

Проект поднимает схему через `Base.metadata.create_all()` (SQLAlchemy).  
Если нужен Alembic — добавьте позже.

## 6. Ограничение доступа

В `.env` заполните:
```
ALLOWED_TELEGRAM_USER_IDS=...
ADMIN_USER_IDS=...
```

## 7. Известные ограничения

- RAG без эмбеддингов — только выбор документов и «учти их в ответе».  
  Добавление pgvector и полноценных эмбеддингов можно сделать на следующем этапе.  
- Генерация изображений требует верифицированной организации в OpenAI.

## 8. Что дальше

- Добавить pgvector + эмбеддинги и семантический поиск  
- Добавить поддержку паролей к PDF  
- Вынести выбор модели в InlineKeyboard  
- Добавить транскрибацию и TTS (Whisper / TTS API OpenAI)
