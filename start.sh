#!/bin/bash
set -e
# Apply database migrations
if [ -f "alembic.ini" ]; then
    alembic upgrade head || true
fi
# Start the Telegram bot
python -m bot.main
