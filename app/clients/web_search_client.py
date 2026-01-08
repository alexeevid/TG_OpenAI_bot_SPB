# app/clients/web_search_client.py
from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

log = logging.getLogger(__name__)


class WebSearchClient:
    """
    Веб-поиск через Tavily.

    Поддерживаем provider:
      - "tavily"
      - "auto" (если есть ключ tavily -> используем tavily)
      - "disabled"
    """

    def __init__(
        self,
        provider: str | None,
        *,
        tavily_api_key: str = "",
        enabled: bool = False,
        timeout_s: float = 15.0,
    ):
        self.provider = (provider or "disabled").strip().lower()
        self.enabled = bool(enabled)
        self.tavily_api_key = (tavily_api_key or "").strip()
        self.timeout_s = float(timeout_s)

    def _resolved_provider(self) -> str:
        """
        Решаем, какой провайдер реально использовать.
        """
        if not self.enabled:
            return "disabled"

        if self.provider in ("disabled", "off", "false", "0"):
            return "disabled"

        if self.provider == "tavily":
            return "tavily"

        # AUTO: если ключ есть — берём tavily
        if self.provider in ("auto", "default", ""):
            if self.tavily_api_key:
                return "tavily"
            return "disabled"

        # неизвестный провайдер
        return "disabled"

    def search(self, query: str, *, max_results: int = 7) -> List[Dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []

        provider = self._resolved_provider()
        if provider == "disabled":
            # Важно: не спамим логами на каждый запрос, но один раз подсказка полезна
            if self.enabled and self.provider != "disabled":
                log.warning(
                    "WebSearchClient disabled: provider=%s resolved=%s key_present=%s",
                    self.provider,
                    provider,
                    bool(self.tavily_api_key),
                )
            return []

        if provider != "tavily":
            log.warning("WebSearchClient: provider '%s' is not supported", provider)
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
