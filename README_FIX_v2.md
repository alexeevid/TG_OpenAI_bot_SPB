# Hotfix v2: команды /img, /dialogs, /reset + unknown fallback

Дата: 2025-09-20

## Что исправляет
- **0 реакции на /img, /dialogs, /reset** — команды автоматически регистрируются даже если они не были добавлены в `Application`.
- Публикуется меню команд (setMyCommands): `/help`, `/img`, `/dialogs`, `/reset`.
- Генерация изображений `/img` использует ваш `openai_helper` (`image_generate` / `images_generate` / `create_image`) при наличии.

## Как работает
- `sitecustomize.py` автоматически грузит:
  - `services/missing_impl.py` — функции `embed_query`, `_build_prompt`, `_llm_answer_*`
  - `services/patch_ptb_commands.py` — монкипатчит `ApplicationBuilder.build()` из PTB v20 и **добавляет хендлеры**.
- Мы **не меняем ваши файлы**, патч активируется при старте Python.

## Установка
1. Распакуйте архив **в корень репозитория** (рядом с `bot/`, `handlers/`).
2. Задеплойте. После старта бот опубликует команды и начнёт отвечать на `/img`, `/dialogs`, `/reset`.
3. Если ранее ставили v1 — можно просто заменить файлами v2.

## Примечания
- `/dialogs` и `/reset` пытаются найти ваши функции (например, `list_dialogs(chat_id)`, `reset_dialog(chat_id)`) в модулях `bot.dialogs`, `dialogs`, `bot.db`, `db`. 
  - Если найдены — вызываются.
  - Если нет — бот всё равно ответит, но сброс будет только в оперативной памяти, а список диалогов не подтянется из БД.
- `/img`:
  - Ищет в `openai_helper` одну из функций: `image_generate`, `images_generate`, `create_image`. Возвращает `url` или `b64_json`.
  - Если ничего не найдено — отправит понятное сообщение об ошибке вместо молчания.
