# TECH\_SPEC — Telegram‑бот с OpenAI, **RAG на PostgreSQL + PGVector** и интеграцией с Яндекс.Диском

> Версия: **v1.1 (обновлено 2025-08-10)**\
> Цель: единая спецификация, по которой можно собрать бота с нуля.

---

## 1) Краткое описание

Telegram‑бот, который отвечает на вопросы с учётом внешней **Базы знаний (БЗ)**, лежащей на **Яндекс.Диске**. Тексты из документов индексируются в **PostgreSQL** с расширением **PGVector** для векторного поиска (RAG). Бот поддерживает мультимодальность (текст, голос, изображения), многодиалоговость, цитирование источников и строгую систему доступа (админы/пользователи/гости).

---

## 2) Архитектура решения (высокоуровневая)

```
User ──> Telegram API ──> python‑telegram‑bot ──> ChatGPTTelegramBot
                                          │
                                          ├─> OpenAI (chat, embeddings, images, whisper)
                                          │
                                          ├─> Яндекс.Диск API (список/скачивание файлов)
                                          │
                                          └─> PostgreSQL + PGVector (диалоги, метаданные БЗ, чанки, эмбеддинги)
```

### 2.1 Модули проекта

- **main.py** — инициализация настроек, логов, БД, запуск polling; регистрация хендлеров.
- **bot/telegram\_bot.py** — класс `ChatGPTTelegramBot`: команды, callbacks, on\_error.
- **bot/openai\_helper.py** — клиенты и обёртки для Chat Completions, Embeddings, Whisper, Image gen.
- **bot/knowledge\_base/**
  - `indexer.py` — синхронизация с Яндекс.Диском, парсинг, чанкование, подсчёт эмбеддингов, запись в PGVector.
  - `retriever.py` — поиск релевантных чанков (similarity search), формирование контекста.
- **bot/yandex\_client.py** / **bot/yandex\_rest.py** — работа с API Яндекс.Диска (листинг, скачивание, HEAD/etag).
- **bot/dialog\_manager.py** — диалоги/сообщения пользователя (CRUD в БД), состояния диалога.
- **bot/db/** — SQLAlchemy модели, сессии, миграции (Alembic), хелперы.
- **bot/settings.py** — Pydantic‑настройки, чтение env, валидация.
- **bot/utils/** — общее: chunking, pdf/docx/xlsx/pptx извлечение, нормализация текста, токенайзер, троттлинг, формат ответа с цитатами.

---

## 3) Переменные окружения (env)

| Имя                      | Тип | Обяз. | Пример                       | Описание                                           |
| ------------------------ | --- | ----- | ---------------------------- | -------------------------------------------------- |
| TELEGRAM\_BOT\_TOKEN     | str | ✔     | 12345\:ABC                   | Токен Telegram бота                                |
| OPENAI\_API\_KEY         | str | ✔     | sk‑...                       | API‑ключ OpenAI                                    |
| OPENAI\_MODEL            | str | ✔     | gpt‑4o‑mini                  | Модель чата по умолчанию                           |
| OPENAI\_IMAGE\_MODEL     | str | ✔     | dall‑e‑3                     | Модель генерации изображений                       |
| OPENAI\_EMBEDDING\_MODEL | str | ✔     | text‑embedding‑3‑large       | Модель эмбеддингов для PGVector                    |
| DATABASE\_URL            | str | ✔     | postgresql://u\:p\@h:5432/db | Подключение к PostgreSQL                           |
| YANDEX\_DISK\_TOKEN      | str | ✔     | y0\_AgAAA...                 | OAuth токен Я.Диска                                |
| YANDEX\_ROOT\_PATH       | str | ✔     | /KnowledgeBase               | Корневая папка БЗ на диске                         |
| ADMIN\_USER\_IDS         | str | ✔     | 111,222                      | CSV id админов                                     |
| ALLOWED\_USER\_IDS       | str | ✖     | 333,444                      | CSV id разрешённых (если пусто — все пользователи) |
| CHUNK\_SIZE              | int | ✖     | 1200                         | Длина чанка в символах                             |
| CHUNK\_OVERLAP           | int | ✖     | 200                          | Перекрытие чанков                                  |
| KB\_TOP\_K               | int | ✖     | 5                            | Сколько чанков тащим в prompt                      |
| MAX\_CONTEXT\_TOKENS     | int | ✖     | 6000                         | Бюджет токенов на контекст из БЗ                   |
| LOG\_LEVEL               | str | ✖     | INFO                         | Уровень логирования                                |
| RATE\_LIMIT\_PER\_MIN    | int | ✖     | 20                           | Антиспам на пользователя                           |

---

## 4) Данные и БД (PostgreSQL + PGVector)

Используем **pgvector** (`CREATE EXTENSION IF NOT EXISTS vector;`). Размерность соответствует модели эмбеддингов (например, 3072 для `text-embedding-3-large`).

### 4.1 Таблицы (минимальный состав)

- **users**: `id (pk)`, `tg_user_id (unique)`, `is_admin bool`, `is_allowed bool`, `lang`, `created_at`.
- **dialogs**: `id (pk)`, `user_id (fk users)`, `title`, `style`, `model`, `is_deleted`, `created_at`, `last_message_at`.
- **messages**: `id (pk)`, `dialog_id (fk dialogs)`, `role` (user/assistant/system), `content`, `tokens`, `created_at`.
- **kb\_documents**: `id (pk)`, `path` (уникальный путь на Я.Диске), `etag`, `mime`, `pages`, `bytes`, `updated_at`, `is_active`.
- **kb\_chunks**: `id (pk)`, `document_id (fk kb_documents)`, `chunk_index`, `content`, `meta` (json: страница/слайд/ячейка), `embedding vector(3072)`.
- **dialog\_kb\_links**: `id (pk)`, `dialog_id (fk)`, `document_id (fk)`, `created_at` (подключение дока к диалогу).
- **pdf\_passwords**: `id (pk)`, `dialog_id (fk)`, `document_id (fk)`, `pwd_hash` (опционально), `created_at`.
- **audit\_log**: `id`, `user_id`, `event`, `payload jsonb`, `created_at` (для важных действий: grant/revoke, sync, purge).

Индексы: `kb_chunks using ivfflat (embedding vector_cosine_ops)`; B‑Tree на `path`, `dialog_id`, `user_id`.

### 4.2 Ограничения и объёмы

- Число чанков на документ — ориентир 200–5 000 (зависит от размера).
- Жёсткий лимит `MAX_CONTEXT_TOKENS`: перед вставкой чанков в prompt, обрезаем по токенам.

---

## 5) Синхронизация БЗ (Indexer)

**Триггеры:** `/kb_sync` (админ), или cron/interval за счёт фоновой задачи (по умолчанию вручную).

**Алгоритм:**

1. Получаем список файлов из `YANDEX_ROOT_PATH` (плоский/рекурсивный режим — параметризуемо).
2. Фильтруем по типам: `.pdf` (в т.ч. защищённые), `.docx`, `.xlsx`, `.pptx`, `.txt`, `.csv`.
3. Для каждого файла:
   - Сверяем `etag` (или last‑modified/size) с тем, что у нас в `kb_documents`.
   - Если новый/изменился → скачиваем во временную папку, парсим (поддержка паролей для PDF — **только при подключении к диалогу**, пароль не хранится глобально).
   - Нормализуем текст (удаляем мусор, переносы, много пробелов), режем на чанки (`CHUNK_SIZE` + `CHUNK_OVERLAP`).
   - Считаем эмбеддинги на **OpenAI\_EMBEDDING\_MODEL** (батчами, с ретраями).
   - Пишем чанки в `kb_chunks` и документ/метадату в `kb_documents` (upsert).
4. Для удалённых на Диске файлов — **ставим **``; при следующем обращении такие документы не участвуют в выдаче.

**Ошибки/ретраи:** сеть, 5xx от API, timeouts → экспоненциальная задержка, запись в `audit_log`.

---

## 6) Поиск и вставка контекста (Retriever, RAG)

**Вход:** пользовательский запрос + список подключённых к диалогу документов.\
**Шаги:**

1. Считаем эмбеддинг запроса (`embedding(model=OPENAI_EMBEDDING_MODEL)`).
2. Достаём top‑K чанков из `kb_chunks` по документам, подключённым к диалогу:\
   `SELECT ... ORDER BY embedding <=> :query_vec LIMIT :KB_TOP_K` (cosine/inner product).
3. Формируем **контекст** (concatenate), соблюдая `MAX_CONTEXT_TOKENS` (сначала самые релевантные, затем менее релевантные; при необходимости применяем rerank).
4. Генерируем **промпт** с системной инструкцией + историей диалога + **контекстом из БЗ**.
5. Запускаем Chat Completion на `OPENAI_MODEL`.
6. В ответ **включаем цитаты** (короткие выдержки) и **источники** (имя документа + локация страницы/слайда/ячейки).
7. Логируем факт использования источников (для дебага качества).

**Важно:**

- Если БЗ **выключена** в данном диалоге — работаем как обычный чат без контекста.
- Если БЗ включена, но **нет релевантных чанков** — честно говорим об этом и отвечаем без «галлюцинаций».

---

## 7) Управление доступом и ролями

- **Админы**: заданы через `ADMIN_USER_IDS` и/или помечены в `users.is_admin=true`.
- **Разрешённые пользователи**:
  - если `ALLOWED_USER_IDS` пусто — **разрешить всем**;
  - иначе — доступ только тем, чьи id в списке или `users.is_allowed=true`.
- **Команды управления доступом (только админ):**
  - `/grant <tg_id>` — добавить пользователя в `users` и поставить `is_allowed=true`.
  - `/revoke <tg_id>` — поставить `is_allowed=false`.
  - `/whoami` — показать права текущего пользователя.
- При любом апдейте, если пользователь не разрешён → вежливое сообщение и лог в `audit_log`.

---

## 8) Команды и сценарии (детально)

### 8.1 Общие

- `/start` — приветствие, создание первого диалога; краткая памятка.
- `/help` — краткая шпаргалка по командам с примерами; пагинация через inline‑кнопки.
- `/reset` — сброс активного диалога (история сообщений и временные состояния: ожидание пароля, выбор документа).
- `/stats` — кол‑во диалогов, сообщений, использованных токенов (по желанию) и подключённых документов.

### 8.2 Диалоги

- `/dialogs` — список диалогов (inline‑кнопки: открыть, переименовать, экспорт `.md`, удалить).
- `/dialog <id>` — сделать активным.
- Автогенерация названия: `YYYY‑MM‑DD | <первые слова вопроса>`.
- Экспорт истории диалога в `.md` (вставлять цитаты/источники опционально).

### 8.3 Модель и стиль

- `/model` — показать список доступных моделей из OpenAI API (list), выбор inline‑кнопками, сохранение в `dialogs.model`.
- `/mode` — стиль ответа: `ceo`, `expert`, `pro`, `user` (системная инструкция меняется соответственно, хранить в `dialogs.style`).

### 8.4 KB (Яндекс.Диск)

- `/kb` — меню:
  - **Выбрать документы** (пагинация, чекбоксы)
  - **Мои в диалоге** (список подключённых + снятие)
  - **Синхронизация** (только для админа)
- Подключение/снятие документа выполняется записью/удалением строки в `dialog_kb_links`.
- Если документ `.pdf` и защищён — бот запрашивает пароль **при первом использовании**, хранит в `pdf_passwords` **только для этого диалога** (можно хранить хеш/шифр).

### 8.5 Диагностика БЗ

- `/kb_diag` (админ): количество документов/чанков; список последней синхронизации; orphan‑чанки; dangle‑links; наличие неактивных документов, которые подключены к диалогам.

### 8.6 Изображения и голос

- `/img <prompt>` — генерация изображения через `OPENAI_IMAGE_MODEL`, отдаём как фото.
- **Голос**: при `voice`‑сообщении — скачиваем `ogg`, шлём в Whisper → текст; далее обычный RAG ответ.

### 8.7 Веб‑поиск

- `/web <запрос>` — поиск по внешнему API (Bing/SerpAPI): короткий дайджест + источники.
- Переключатель «включить веб‑поиск по умолчанию» — флаг в state диалога.

---

## 9) Формирование ответа (prompting)

**Системная инструкция** (настраивается стилем `/mode`), далее:

- **История** последних N сообщений (N по бюджету токенов, напр. 6–10).
- **Контекст из БЗ** (чанки с цитатами, `KB_TOP_K`, соблюдаем `MAX_CONTEXT_TOKENS`).
- **Запрос пользователя**.

**Требования к ответу:**

- Если использована БЗ — привести 2–5 цитат с короткими ссылками на **Источник**: `Документ (страница X / слайд Y / лист Z)`.
- Если релевантных фрагментов нет — явно указать это и не галлюцинировать.

---

## 10) Обработка ошибок и надёжность

- **Глобальный **``: логирование stacktrace, отправка пользователю «что‑то пошло не так», не падаем.
- **Ретраи** на сетевых ошибках (OpenAI, Я.Диск) с экспоненциальной паузой.
- Валидация входных данных (mimetype/размер файла, пустые ответы, timeouts).
- Троттлинг: `RATE_LIMIT_PER_MIN` на пользователя (in‑memory/Redis/DB счётчики).

---

## 11) Railway: ограничения и настройки

- Python 3.11, `psycopg2-binary`, `sqlalchemy`, `pgvector`, `python-telegram-bot==20.*`.
- `WEBHOOK` не обязателен — используем `run_polling()` (проще для старта).
- Логи по умолчанию `INFO`. Для отладки — `DEBUG` (может стоить денег на OpenAI из‑за подробностей; включать только временно).
- Убедиться, что в PostgreSQL установлен `pgvector` и настроены индексы `ivfflat`.

---

## 12) Security & Privacy

- Не хранить пароли к PDF в открытом виде (минимум: хеш; лучше — шифр с ключом из env).
- Секреты только в env, не коммитить.
- Логи без чувствительных данных (маскирование токенов).

---

## 13) План работ (итерации)

**Итерация 1 (инфраструктура):** миграции БД, pgvector, базовые модели, DialogManager, settings, on\_error.\
**Итерация 2 (Я.Диск + индексатор):** синхронизация, парсеры, чанкование, эмбеддинги, запись в PGVector.\
**Итерация 3 (RAG‑ответы):** retriever, бюджет токенов, цитирование, стили `/mode`.\
**Итерация 4 (UI в TG):** `/kb` с пагинацией, «мои документы», пароли к PDF, `/kb_diag`, `/dialogs`.\
**Итерация 5 (Мультимодальность):** `/img`, голос, `/web`, экспорт диалога.\
**Итерация 6 (Доступ и наблюдаемость):** `/grant`, `/revoke`, `/whoami`, audit\_log, троттлинг.\
**Итерация 7 (Оптимизация):** индексы, батчинги, кэш эмбеддингов, ограничения по токенам.

---

## 14) Открытые вопросы (для уточнения)

1. Сканировать поддиректории `YANDEX_ROOT_PATH` рекурсивно или только 1 уровень?
2. Формат цитат/ссылок в ответах (только имя файла + страница? добавлять прямую ссылку на файл в Я.Диске?).
3. Размер окна истории (по токенам/по сообщениями).
4. Нужна ли отдельная роль «модератор» (только просмотр `/kb_diag`, без `/grant`/`/revoke`)?

---

## 15) Приложение: псевдо‑SQL для PGVector

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE kb_documents (
  id BIGSERIAL PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  etag TEXT,
  mime TEXT,
  pages INT,
  bytes BIGINT,
  updated_at TIMESTAMPTZ DEFAULT now(),
  is_active BOOLEAN DEFAULT TRUE
);

-- Размерность подберите под выбранную модель эмбеддингов
CREATE TABLE kb_chunks (
  id BIGSERIAL PRIMARY KEY,
  document_id BIGINT REFERENCES kb_documents(id) ON DELETE CASCADE,
  chunk_index INT NOT NULL,
  content TEXT NOT NULL,
  meta JSONB,
  embedding VECTOR(3072)
);

CREATE INDEX ON kb_chunks (document_id);
CREATE INDEX kb_chunks_embedding_idx ON kb_chunks USING ivfflat (embedding vector_cosine_ops);
```

---

**Готово.** Это детализированная спецификация с учётом **PGVector** и потоков данных. Предлагаю дальше:

- добавить ER‑диаграмму и Alembic‑миграции,
- описать форматы сообщений бота (шаблоны текстов и клавиатуры),
- зафиксировать конкретные модели OpenAI (production/backup).



---

## 16) ER‑диаграмма (ASCII)

```
users (id PK) ──┐
                │ 1..N
dialogs (id PK, user_id FK -> users.id)
   │ 1..N
   └── messages (id PK, dialog_id FK)

kb_documents (id PK)
   │ 1..N
   └── kb_chunks (id PK, document_id FK)

dialogs (id) 1..N ── dialog_kb_links (id PK, dialog_id FK, document_id FK) N..1 ── kb_documents (id)

dialogs (id) 1..N ── pdf_passwords (id PK, dialog_id FK, document_id FK) N..1 ── kb_documents (id)

users (id) 1..N ── audit_log (id PK, user_id FK)
```

---

## 17) Alembic: стартовый набор миграций

**env.py**: стандартный шаблон под SQLAlchemy + DATABASE\_URL из env.\
**versions/**:

- `0001_init_core.py`
  - `users`, `dialogs`, `messages`.
- `0002_kb_pgvector.py`
  - `CREATE EXTENSION IF NOT EXISTS vector;`
  - `kb_documents`, `kb_chunks (VECTOR(3072))`, индекс `ivfflat`.
- `0003_dialog_kb_links_passwords.py`
  - `dialog_kb_links`, `pdf_passwords`.
- `0004_audit_log.py`
  - `audit_log`.

**Пример up() для 0002:**

```python
from alembic import op
import sqlalchemy as sa


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.create_table(
        'kb_documents',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('path', sa.Text, nullable=False, unique=True),
        sa.Column('etag', sa.Text),
        sa.Column('mime', sa.Text),
        sa.Column('pages', sa.Integer),
        sa.Column('bytes', sa.BigInteger),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('is_active', sa.Boolean, server_default=sa.text('true')),
    )
    op.create_table(
        'kb_chunks',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('document_id', sa.BigInteger, sa.ForeignKey('kb_documents.id', ondelete='CASCADE')),
        sa.Column('chunk_index', sa.Integer, nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('meta', sa.JSON),
        sa.Column('embedding', sa.dialects.postgresql.VECTOR(dim=3072)),
    )
    op.create_index('ix_kb_chunks_document_id', 'kb_chunks', ['document_id'])
    op.execute("CREATE INDEX kb_chunks_embedding_idx ON kb_chunks USING ivfflat (embedding vector_cosine_ops);")


def downgrade():
    op.execute('DROP INDEX IF EXISTS kb_chunks_embedding_idx;')
    op.drop_index('ix_kb_chunks_document_id', table_name='kb_chunks')
    op.drop_table('kb_chunks')
    op.drop_table('kb_documents')
```

> ⚠️ Если используете Alembic <1.12, тип VECTOR может потребовать кастомный TypeDecorator.

---

## 18) Шаблоны сообщений и клавиатур

### 18.1 Общие тексты

- **Приветствие**: `Привет! Я помогу искать ответы в документах из Базы знаний. Наберите вопрос или откройте /kb.`
- **Отказ в доступе**: `Доступ ограничён. Обратитесь к администратору.`
- **Сбой**: `⚠ Что‑то пошло не так. Попробуйте ещё раз позже.`
- **Нет релевантных фрагментов**: `По подключённым документам не нашёл релевантных фрагментов. Могу ответить общими знаниями или уточните вопрос.`

### 18.2 Клавиатуры (/kb)

- Главное меню:\
  `[["📚 Выбрать документы"], ["🗂 Мои в диалоге"], ["🔄 Синхронизация"(только админ)]]`
- Список документов (страница):\
  `[["☐ Документ 1"],["☐ Документ 2"], ..., ["« Назад", "Вперёд »"], ["💾 Сохранить"], ["⬅️ В меню БЗ"]]`
- Подключённые:\
  `[["Снять: Документ 1"],..., ["⬅️ В меню БЗ"]]`

### 18.3 Диалоги

- Список: кнопки `Открыть | ✏️ | 📤 | 🗑️`.
- Переименование: `Введите новое название диалога:`.
- Экспорт: отдаём `.md` с заголовком и сообщениями в порядке времени.

---

## 19) Системные промпты и стили `/mode`

### 19.1 Базовая системная инструкция

```
Ты — аккуратный ассистент. Отвечай по сути. Если используется База знаний, обязательно ссылайся на источники (2–5 цитат короткими строками). Если уверенности нет — скажи об этом и предложи варианты уточнения.
```

### 19.2 Стили

- **ceo**: кратко, стратегично, фокус на рисках и решениях, 3–5 буллетов, максимум пользы за минимум слов.
- **expert**: глубоко, со ссылками на источники, короткие цитаты, причины/следствия, таблички по необходимости.
- **pro**: структурно, чётко, шаги внедрения, чек‑листы.
- **user**: простыми словами, примеры, аналогии.

**Подстановка в системное сообщение:**

```
Стиль: {style}. Язык интерфейса: {lang}. Не выдумывай факты. В конце — краткое резюме из 1–2 строк.
```

---

## 20) Бюджет токенов и компоновка prompt

- История диалога: до 6–10 последних сообщений **или** лимит в 2 000–3 000 токенов.
- Контекст из БЗ: до `MAX_CONTEXT_TOKENS` (напр. 6 000) — набираем top‑K чанки, обрезаем по токенам.
- Пользовательский запрос: без изменений.
- Системка: фикс.

> Рекомендация: считать токены токенизатором tiktoken/по аналогу, и «уплотнять» длинные чанки.

---

## 21) Псевдокод Retriever (postgres + pgvector)

```sql
-- query_embedding = embed(user_question)
SELECT c.content, c.meta, d.path
FROM kb_chunks c
JOIN kb_documents d ON d.id = c.document_id AND d.is_active
JOIN dialog_kb_links l ON l.document_id = d.id AND l.dialog_id = :dialog_id
ORDER BY c.embedding <=> :query_embedding
LIMIT :top_k;
```

Далее — сортировка/агрегация на стороне Python, сборка контекста до лимита токенов.

---

## 22) Контроль доступа: уточнение

- Хук «перед обработкой update»:\
  Если `ALLOWED_USER_IDS` пусто — пропускаем всех.\
  Иначе — пропускаем только тех, кто указан **или** уже помечен `users.is_allowed=true`.
- Команды только для админов:
  - `/grant <tg_id>` → `users.is_allowed=true` (создать пользователя, если нет).
  - `/revoke <tg_id>` → `users.is_allowed=false`.
  - `/whoami` → вывести `is_admin`, `is_allowed`, текущий диалог, стиль, модель.

---

## 23) Следующие шаги

1. Подготовить Alembic‑миграции по шаблонам (п.17).
2. Реализовать `indexer.py` (потоки: list → download → parse → chunk → embed → upsert).
3. Реализовать `retriever.py` (SQL + агрегация контекста по токенам).
4. Внедрить `/kb` UI (пагинация, выбор, «мои документы», пароли PDF).
5. Подключить `/mode`, `/model`, `/dialogs`, экспорт `.md`.
6. Добавить `/img`, голос (Whisper), `/web`.

