#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
echo '🔧 Running Alembic migrations...'
set +e
alembic upgrade head
rc=$?
set -e
if [ $rc -ne 0 ]; then
  echo '⚠️ Alembic upgrade failed, stamping initial revision and retrying...'
  alembic stamp 001_initial
  alembic upgrade head
fi
echo '🚀 Starting bot...'
exec python -m bot.main
