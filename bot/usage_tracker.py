# bot/usage_tracker.py
from typing import Optional
from bot.db.session import SessionLocal
from bot.db.models import Message

class UsageTracker:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def track(self,
              chat_id: int,
              user_id: Optional[int],
              model: Optional[str],
              prompt_tokens: Optional[int],
              completion_tokens: Optional[int],
              total_tokens: Optional[int],
              cost_usd: Optional[float] = None,
              content: Optional[str] = None):
        if not self.enabled:
            return
        with SessionLocal() as s:
            s.add(Message(
                chat_id=chat_id,
                user_id=user_id,
                role="assistant",
                content=content or "",
                tokens_prompt=prompt_tokens,
                tokens_completion=completion_tokens,
                total_tokens=total_tokens,
                model=model,
                cost_usd=cost_usd,
            ))
            s.commit()
