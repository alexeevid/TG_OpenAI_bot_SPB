
# Telegram OpenAI RAG Bot (Yandex Disk, no pgvector) — fixed KB UI

Что исправлено по сравнению с предыдущей сборкой:
- Исправлен TypeError в /kb (Message не вызывался как функция)
- Добавлен глобальный error handler (не будет "No error handlers are registered")
- Чёткое различение Update vs CallbackQuery в `_send_kb_list`
- /kb_sync по-прежнему только для админов (ADMIN_USER_IDS)
- Всё остальное поведение неизменно

См. .env.example — заполните переменные окружения.
