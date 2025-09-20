# bot/idempotency.py
from __future__ import annotations
from collections import deque
from typing import Deque, Set

class RecentUpdates:
    def __init__(self, maxlen: int = 1000) -> None:
        self._dq: Deque[int] = deque(maxlen=maxlen)
        self._set: Set[int]  = set()

    def seen(self, update_id: int) -> bool:
        """True, если такой update уже обрабатывали (и пометим как обработанный)."""
        if update_id in self._set:
            return True
        self._dq.append(update_id)
        self._set.add(update_id)
        if len(self._dq) == self._dq.maxlen:
            # выкидываем старые
            while len(self._set) > len(self._dq):
                self._set.discard(self._dq[0])
        return False

# Глобальный инстанс
recent_updates = RecentUpdates(maxlen=1000)
