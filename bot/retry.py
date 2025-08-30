# bot/retry.py
from __future__ import annotations
import asyncio, random
from typing import Callable, Awaitable, Tuple, Type

async def retry_async(
    func: Callable[[], Awaitable],
    *,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    tries: int = 3,
    base_delay: float = 0.6,
    max_delay: float = 6.0,
) -> any:
    """Повторяем func() с экспоненциальной задержкой и джиттером."""
    attempt = 0
    while True:
        try:
            return await func()
        except exceptions:
            attempt += 1
            if attempt >= tries:
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = delay * (0.7 + random.random() * 0.6)  # джиттер 70–130%
            await asyncio.sleep(delay)
