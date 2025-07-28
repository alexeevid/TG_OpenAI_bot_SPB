# CHANGELOG

## 2025-07-28
- Полная реконструкция кода для стабильной работы на Railway
- Синхронный `run_polling` (без конфликтов event loop)
- Только `psycopg2-binary` (без libpq)
- Команды меню: /start, /help, /reset, /stats, /kb, /model, /dialogs
- Выбор модели через список с OpenAI
- Тоггл включения/исключения документов БЗ в диалоге
- Возврат к конкретному диалогу (/dialogs, /dialog <id>)
