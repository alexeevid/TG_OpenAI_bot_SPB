# TG_OpenAI_bot_SPB – Fixed build (2025-09-17T06:09:55.078109Z)

Исправления:
- Текст/голос: устойчивое распознавание, RAG, сохранение истории, лимиты токенов.
- /kb: активный диалог, переключение документов, «📎 Сбросить все документы».
- /stats: корректные данные по активному диалогу.
- SQL: сохранение сообщений в колонку `content`, без StatementError.
- Мелочи: убран лишний «Принято…», защита от BadRequest: Message is not modified.

Переменные окружения: DATABASE_URL, OPENAI_API_KEY, OPENAI_TEMPERATURE, MAX_CONTEXT_TOKENS, MAX_KB_CHUNKS, CHUNK_SIZE, CHUNK_OVERLAP.

Деплой на Railway: загрузите архив в репо, проверьте ENV, перезапустите.
