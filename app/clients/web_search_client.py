# app/clients/web_search_client.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger(__name__)


class WebSearchClient:
    """
    Реальный веб-поиск через Tavily.

    Settings уже есть в вашем проекте (25):
      - enable_web_search: bool
      - web_search_provider: str  (например, "tavily")
      - tavily_api_key: str
    """

    def __init__(
        self,
        provider: str | None,
        *,
        tavily_api_key: str = "",
        enabled: bool = False,
        timeout_s: float = 15.0,
    ):
        self.provider = (provider or "disabled").lower()
        self.enabled = bool(enabled)
        self.tavily_api_key = tavily_api_key or ""
        self.timeout_s = float(timeout_s)

    def search(self, query: str, *, max_results: int = 7) -> List[Dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []

        if not self.enabled or self.provider == "disabled":
            return []

        if self.provider != "tavily":
            log.warning("WebSearchClient: provider '%s' is not supported", self.provider)
            return []

        if not self.tavily_api_key:
            log.warning("WebSearchClient: TAVILY_API_KEY is empty")
            return []

        return self._tavily_search(q, max_results=max_results)

    def _tavily_search(self, query: str, *, max_results: int) -> List[Dict[str, Any]]:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": self.tavily_api_key,
            "query": query,
            "max_results": int(max_results),
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }

        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                r = client.post(url, json=payload)
                r.raise_for_status()
                data = r.json() if r.content else {}
        except Exception as e:
            log.exception("Tavily search failed: %s", e)
            return []

        results = data.get("results") or []
        out: List[Dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "title": (item.get("title") or "").strip(),
                    "url": (item.get("url") or "").strip(),
                    "snippet": (item.get("content") or "").strip(),
                }
            )

        return out
