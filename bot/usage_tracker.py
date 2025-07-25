
import logging
from typing import Optional, Any

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
              message: Optional[str] = None,
              extra: Optional[dict[str, Any]] = None):
        if not self.enabled:
            return
        logging.debug(
            "USAGE: chat=%s user=%s model=%s total=%s prompt=%s completion=%s cost=%s",
            chat_id, user_id, model, total_tokens, prompt_tokens, completion_tokens, cost_usd
        )
