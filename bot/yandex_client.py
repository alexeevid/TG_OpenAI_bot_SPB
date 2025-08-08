from __future__ import annotations
import requests
from typing import Dict, Any, List
from bot.settings import load_settings

BASE = "https://cloud-api.yandex.net/v1/disk"
_settings = load_settings()
HEADERS = {"Authorization": f"OAuth {_settings.yandex_disk_token}"}

def list_files(path: str) -> List[Dict[str, Any]]:
    url = f"{BASE}/resources"
    params = {"path": path, "limit": 1000}
    r = requests.get(url, headers=HEADERS, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    items = data.get("_embedded", {}).get("items", [])
    out = []
    for it in items:
        if it.get("type") == "file":
            out.append({
                "path": it.get("path"),
                "name": it.get("name"),
                "mime": it.get("mime_type"),
                "size": it.get("size"),
                "etag": it.get("md5") or it.get("sha256"),
            })
    return out

def download_to_bytes(path: str) -> bytes:
    url = f"{BASE}/resources/download"
    params = {"path": path}
    r = requests.get(url, headers=HEADERS, params=params, timeout=60)
    r.raise_for_status()
    href = r.json().get("href")
    r2 = requests.get(href, timeout=120)
    r2.raise_for_status()
    return r2.content
