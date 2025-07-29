# Telegram Assistant Bot

Полный минимальный проект Telegram‑бота с OpenAI (Responses API), Whisper, генерацией изображений, веб‑поиском и управлением диалогами.

## Запуск локально

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN=...
export OPENAI_API_KEY=...
export DATABASE_URL=postgresql://user:pass@host:5432/dbname
# (опционально) export OPENAI_MODEL=gpt-4o
# (опционально) export IMAGE_MODEL=dall-e-3

python -m bot.main
```

## Деплой на Railway

- Добавьте переменные окружения: `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `DATABASE_URL`.
- При первом старте таблицы создадутся автоматически.

## Команды

- /help — Помощь
- /reset — Сброс контекста
- /stats — Статистика
- /kb — База знаний (вкл/искл документы) — заглушка, реальную логику можно подключить позже
- /model — Выбор модели OpenAI
- /dialogs — Список диалогов с кнопками Открыть/Удалить (пагинация)
- /image — Генерация изображения
- /web — Вопрос с веб‑поиском
- /style — Режим ответа: Профессиональный/Экспертный/Пользовательский/СЕО
