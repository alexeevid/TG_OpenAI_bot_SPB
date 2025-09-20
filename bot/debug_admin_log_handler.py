
import logging
import asyncio

class TelegramAdminLogHandler(logging.Handler):
    """Send WARNING+ logs to a Telegram admin chat via bot API object."""
    def __init__(self, bot, chat_id: int, level: int = logging.WARNING):
        super().__init__(level=level)
        self._bot = bot
        self._chat_id = chat_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            asyncio.create_task(self._bot.send_message(chat_id=self._chat_id, text=f"[log] {msg[:4000]}"))
        except Exception:
            pass
