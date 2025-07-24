import requests
from urllib.parse import quote
from typing import Iterator, Tuple
from xml.etree import ElementTree as ET

class YandexDiskClient:
    def __init__(self, token: str, base_url: str = "https://webdav.yandex.ru"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        if token.lower().startswith("oauth "):
            token = token.split(None,1)[1].strip()
        self.session.headers.update({"Authorization": f"OAuth {token}"})

    def iter_files(self, root_path: str) -> Iterator[Tuple[str, int]]:
        if root_path.startswith("disk:"):
            root_path = root_path[5:]
        if not root_path.startswith("/"):
            root_path = "/" + root_path
        url = f"{self.base_url}{quote(root_path)}"
        resp = self.session.request("PROPFIND", url, headers={"Depth":"infinity"})
        if resp.status_code == 401:
            raise RuntimeError("Unauthorized to Yandex Disk")
        resp.raise_for_status()
        ns = {'d': 'DAV:'}
        root = ET.fromstring(resp.text)
        for r in root.findall('d:response', ns):
            href_el = r.find('d:href', ns)
            if href_el is None:
                continue
            href = href_el.text
            if href.endswith('/'):
                continue
            size_el = r.find('.//d:getcontentlength', ns)
            size = int(size_el.text) if size_el is not None else 0
            yield href, size

    def download(self, remote_path: str) -> bytes:
        url = f"{self.base_url}{remote_path}"
        r = self.session.get(url)
        r.raise_for_status()
        return r.content
