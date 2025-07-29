from __future__ import annotations

from typing import List, Dict

class KnowledgeBaseRetriever:
    """
    Заглушка ретривера.
    Реальная реализация должна искать по embeddings/векторному индексу
    и возвращать релевантные фрагменты для контекста.
    """

    def __init__(self, settings) -> None:
        self.settings = settings

    def retrieve(self, query: str, selected_docs: List[str]) -> List[Dict]:
        # Возвращаем список фрагментов; заглушка — пустой
        return []
# placeholder: simple retriever that just returns doc paths you selected
# (You can extend to real embedding search later.)
