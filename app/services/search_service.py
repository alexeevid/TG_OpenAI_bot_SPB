# app/services/search_service.py
from __future__ import annotations

from typing import Any, Dict, List

from ..clients.web_search_client import WebSearchClient


class SearchService:
    """
    Тонкая обёртка над WebSearchClient.
    Возвращает готовые строки для вывода в Telegram.
    """

    def __init__(self, client: WebSearchClient, *, enabled: bool = False):
        self._c = client
        self._enabled = bool(enabled)

    def search(self, query: str, *, max_results: int = 7) -> List[str]:
        q = (query or "").strip()
        if not q:
            return []

        if not self._enabled:
            return []

        raw: List[Dict[str, Any]] = self._c.search(q, max_results=max_results)
        if not raw:
            return []

        lines: List[str] = []
        n = 1
        for item in raw:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            snippet = (item.get("snippet") or "").strip()

            if not title and not url:
                continue

            # В Telegram ссылки кликабельны сами по себе, markdown не обязателен.
            line = f"{n}) {title}" if title else f"{n})"
            if url:
                line += f"\n{url}"
            if snippet:
                # короткий сниппет, чтобы не засорять чат
                sn = snippet.replace("\n", " ").strip()
                if len(sn) > 240:
                    sn = sn[:240].rstrip() + "…"
                line += f"\n— {sn}"
            lines.append(line)
            n += 1

        return lines
