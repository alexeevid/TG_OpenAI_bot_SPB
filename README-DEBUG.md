
# Debug kit: GitHub + Railway + Telegram

## A) GitHub Actions для логов Railway
1. Получите токен: Railway → Account → Tokens → New Token.
2. В GitHub: Settings → Secrets → Actions → New repository secret → **RAILWAY_TOKEN**.
3. Закоммитьте `.github/workflows/*` в репо.
4. Actions → **Railway Logs (manual)** → **Run workflow**. Заберите артефакт `railway-logs` и посмотрите Summary.

## B) Логи в Telegram админу
1. Добавьте `bot/debug_admin_log_handler.py` в проект.
2. В `build_app()`:
   ```python
   from bot.debug_admin_log_handler import TelegramAdminLogHandler
   import logging, os
   ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)
   if ADMIN_CHAT_ID:
       h = TelegramAdminLogHandler(app.bot, ADMIN_CHAT_ID, level=logging.WARNING)
       logging.getLogger().addHandler(h)
   ```
3. Установите env `ADMIN_CHAT_ID=<ваш_tg_id>`.

## C) /diag
1. `from bot.diag_tools import diag_command`
2. `app.add_handler(CommandHandler("diag", diag_command))` в `build_app()`.
