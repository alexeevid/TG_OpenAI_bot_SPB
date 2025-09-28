
from ..clients.web_search_client import WebSearchClient
class SearchService:
    def __init__(self, client: WebSearchClient):
        self._c = client
    def search(self, query: str) -> list[str]:
        return self._c.search(query)
