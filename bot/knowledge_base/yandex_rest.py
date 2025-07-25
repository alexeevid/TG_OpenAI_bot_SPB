import requests
from typing import List, Dict, Any, Generator

API_BASE = "https://cloud-api.yandex.net/v1/disk"

class YandexDiskREST:
    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"OAuth {token}"})

    def _get(self, path: str, **params):
        url = f"{API_BASE}{path}"
        r = self.session.get(url, params=params)
        r.raise_for_status()
        return r.json()

    def list_recursive(self, root_path: str) -> Generator[Dict[str, Any], None, None]:
        # Walk recursively using /resources?path=... and _embedded.items
        stack = [root_path]
        while stack:
            current = stack.pop()
            data = self._get("/resources", path=current, limit=1000)
            if '_embedded' not in data:
                continue
            for item in data['_embedded']['items']:
                if item['type'] == 'dir':
                    stack.append(item['path'])
                else:
                    yield item

    def download(self, path: str) -> bytes:
        meta = self._get("/resources/download", path=path)
        href = meta['href']
        r = self.session.get(href)
        r.raise_for_status()
        return r.content
