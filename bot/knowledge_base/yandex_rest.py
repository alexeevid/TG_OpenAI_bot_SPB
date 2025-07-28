import requests
from typing import List, Dict, Any

API_BASE = "https://cloud-api.yandex.net/v1/disk"

class YandexDiskREST:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"OAuth {token}"})

    def list_all_files(self, root_path: str) -> List[Dict[str, Any]]:
        files: List[Dict[str, Any]] = []

        def walk(path: str):
            url = f"{API_BASE}/resources?path={requests.utils.quote(path)}&limit=10000"
            r = self.session.get(url)
            if r.status_code == 401:
                raise RuntimeError("401 Unauthorized (check token and scope cloud_api:disk.read)")
            r.raise_for_status()
            data = r.json()
            embedded = data.get('_embedded', {}).get('items', [])
            for item in embedded:
                if item.get('type') == 'dir':
                    walk(item.get('path'))
                else:
                    files.append({
                        "path": item.get('path'),
                        "name": item.get('name'),
                        "mime_type": item.get('mime_type'),
                        "size": item.get('size'),
                    })

        walk(root_path)
        return files
