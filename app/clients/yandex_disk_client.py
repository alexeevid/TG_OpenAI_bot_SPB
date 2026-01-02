from __future__ import annotations

import requests
from typing import Any, Dict, List


class YandexDiskClient:
    def __init__(self, token: str | None, root: str | None):
        self.token = token
        self.root = (root or "/kb").rstrip("/")
        self.base = "https://cloud-api.yandex.net/v1/disk"

    def _h(self) -> Dict[str, str]:
        return {"Authorization": f"OAuth {self.token}"} if self.token else {}

    def _full(self, path: str) -> str:
        path = (path or "").strip()
        if not path:
            return self.root
        if path.startswith("/"):
            return path
        return f"{self.root}/{path}".rstrip("/")

    def list(self, path: str) -> Dict[str, Any]:
        if not self.token:
            return {"_stub": True, "items": []}
        url = f"{self.base}/resources"
        r = requests.get(url, headers=self._h(), params={"path": self._full(path), "limit": 1000})
        r.raise_for_status()
        return r.json()

    def download(self, path: str) -> bytes:
        if not self.token:
            return b""
        url = f"{self.base}/resources/download"
        r = requests.get(url, headers=self._h(), params={"path": self._full(path)})
        r.raise_for_status()
        href = r.json()["href"]
        f = requests.get(href)
        f.raise_for_status()
        return f.content

    def list_kb_files_metadata(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.token:
            return out

        def walk(rel: str) -> None:
            data = self.list(rel)
            emb = data.get("_embedded") or {}
            items = emb.get("items") or []
            for it in items:
                it_type = it.get("type")
                p = it.get("path")
                if it_type == "dir":
                    rel_next = p
                    if isinstance(rel_next, str) and rel_next.startswith(self.root):
                        rel_next = rel_next[len(self.root):].lstrip("/")
                    walk(rel_next)
                else:
                    out.append(
                        {
                            "resource_id": it.get("resource_id") or it.get("path"),
                            "path": (p[len(self.root):].lstrip("/") if isinstance(p, str) and p.startswith(self.root) else p),
                            "modified": it.get("modified"),
                            "md5": it.get("md5"),
                            "size": it.get("size"),
                        }
                    )

        walk("")
        return [x for x in out if x.get("path")]
