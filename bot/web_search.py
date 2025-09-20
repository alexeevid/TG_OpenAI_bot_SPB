# bot/web_search.py
import os, httpx
from urllib.parse import urlparse
from openai import OpenAI

async def web_search_digest(query: str, *, max_results: int = 6, openai_api_key: str | None = None):
    """
    Возвращает (answer, sources) где sources=[{title,url,content}]
    Провайдер: Tavily -> SerpAPI -> Bing (что есть в env).
    """
    tavily = os.getenv("TAVILY_API_KEY")
    serp   = os.getenv("SERPAPI_KEY")
    bing   = os.getenv("BING_API_KEY")

    sources = []
    answer = ""

    async with httpx.AsyncClient(timeout=25) as client:
        if tavily:
            r = await client.post("https://api.tavily.com/search", json={
                "api_key": tavily,
                "query": query,
                "search_depth": "advanced",
                "max_results": max_results,
                "include_answer": True,
                "include_domains": [],
                "exclude_domains": [],
            })
            j = r.json()
            sources = [{"title": it.get("title",""), "url": it.get("url",""), "content": it.get("content","")} for it in j.get("results", [])]
            answer = j.get("answer") or ""
        elif serp:
            r = await client.get("https://serpapi.com/search.json", params={
                "engine": "google", "q": query, "api_key": serp, "num": max_results
            })
            j = r.json()
            org = j.get("organic_results") or []
            sources = [{"title": it.get("title",""), "url": it.get("link",""), "content": it.get("snippet","") or ""} for it in org[:max_results]]
        elif bing:
            r = await client.get("https://api.bing.microsoft.com/v7.0/search", params={"q": query, "count": max_results},
                                 headers={"Ocp-Apim-Subscription-Key": bing})
            j = r.json()
            vals = (j.get("webPages") or {}).get("value", [])
            sources = [{"title": it.get("name",""), "url": it.get("url",""), "content": it.get("snippet","") or ""} for it in vals[:max_results]]
        else:
            raise RuntimeError("Нет ключей: установи TAVILY_API_KEY или SERPAPI_KEY или BING_API_KEY")

    if not answer:
        if openai_api_key:
            client = OpenAI(api_key=openai_api_key)
            corpus = "\n\n".join([f"[{i+1}] {s['title']}\n{s['content']}\nURL:{s['url']}" for i, s in enumerate(sources)])
            prompt = (
                "Суммируй информацию по запросу. Кратко и структурно: пункты/подзаголовки.\n"
                "Только проверенные факты из источников. В конце добавь блок «Источники» с [1] [2]...\n\n"
                f"Запрос: {query}\n\nКорпус:\n{corpus}"
            )
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content": prompt}],
                temperature=0.2,
                max_tokens=700,
            )
            answer = resp.choices[0].message.content
        else:
            answer = "Нашёл несколько релевантных источников."

    return answer, sources

def sources_footer(sources):
    from urllib.parse import urlparse
    return "\n".join([f"[{i+1}] {s['title'] or urlparse(s['url']).netloc}" for i, s in enumerate(sources)])
