
import requests
class YandexDiskClient:
    def __init__(self, token: str | None, root: str | None):
        self.token = token
        self.root = (root or "/kb").rstrip('/')
        self.base = "https://cloud-api.yandex.net/v1/disk"
    def _h(self):
        return {"Authorization": f"OAuth {self.token}"} if self.token else {}
    def list(self, path: str):
        if not self.token:
            return {"_stub": True, "items":[]}
        url = f"{self.base}/resources"
        r = requests.get(url, headers=self._h(), params={"path": f"{self.root}/{path}".rstrip('/')})
        r.raise_for_status(); return r.json()
    def download(self, path: str) -> bytes:
        if not self.token:
            return b""
        url = f"{self.base}/resources/download"
        r = requests.get(url, headers=self._h(), params={"path": f"{self.root}/{path}".rstrip('/')})
        r.raise_for_status(); href = r.json()["href"]
        f = requests.get(href); f.raise_for_status(); return f.content
