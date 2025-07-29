from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)

class KnowledgeBaseIndexer:
    """
    Заглушка индексатора БЗ.
    Реальная реализация должна:
      - обойти источник (Я.Диск/локальная папка),
      - посчитать хэши/метаданные,
      - синхронизировать таблицу documents (insert/update/delete).
    Метод sync() возвращает кортеж (added, updated, deleted, unchanged).
    """

    def __init__(self, settings) -> None:
        self.settings = settings

    def sync(self) -> Tuple[int, int, int, int]:
        logger.info("KB sync (stub): nothing to do")
        return (0, 0, 0, 0)
