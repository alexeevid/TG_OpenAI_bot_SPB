
class WebSearchClient:
    def __init__(self, provider: str | None):
        self.provider = provider or "disabled"
    def search(self, query: str) -> list[str]:
        return [f"[{self.provider}] {query}"]
