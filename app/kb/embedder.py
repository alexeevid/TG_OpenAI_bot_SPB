from __future__ import annotations

from typing import List, Sequence

from ..clients.openai_client import OpenAIClient


class Embedder:
    def __init__(self, openai_client: OpenAIClient, model: str):
        self._cli = openai_client
        self._model = model

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        return self._cli.embeddings(texts, model=self._model)
