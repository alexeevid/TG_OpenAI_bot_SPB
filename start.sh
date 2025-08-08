#!/usr/bin/env bash
set -euo pipefail

# гарантия, что пакет bot виден Python-у
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

echo '🔧 Running Alembic migrations...'
alembic upgrade head

echo '🚀 Starting bot...'
exec python -m bot.main
