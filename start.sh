#!/usr/bin/env bash
set -euo pipefail

# Чтобы Alembic и bot виделись как пакет
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

echo '🔧 Running Alembic migrations...'
alembic upgrade head

echo '🚀 Starting bot...'
exec python -m bot.main
