#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

echo '🔧 Running Alembic migrations...'
set +e
alembic upgrade head
rc=$?
set -e

if [ $rc -ne 0 ]; then
  echo "⚠️ Alembic upgrade failed, stamping initial revision and retrying..."
  # Сообщаем Alembic, что стартовая ревизия уже применена
  alembic stamp 001_initial
  # На всякий случай ещё раз апгрейд (если появятся новые ревизии позже)
  alembic upgrade head
fi

echo '🚀 Starting bot...'
exec python -m bot.main
