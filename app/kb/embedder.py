
class Embedder:
    def __init__(self, openai_client, model: str):
        self._cli = openai_client
        self._model = model
    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._cli.embeddings(texts, self._model)
