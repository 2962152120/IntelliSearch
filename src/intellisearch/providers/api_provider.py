"""商业检索 API 源(可选)。

配置了对应 API Key 才会自动启用; 相比 HTML 抓取, 这类源:
- 返回结构化 JSON, 不受页面改版影响;
- 有官方配额与 SLA, 适合生产环境;
- 通常需要付费。

支持: Tavily / SerpAPI / Brave Search。
"""
from __future__ import annotations

from typing import List

from ..errors import UpstreamError
from ..models import QueryOptions, SearchResult, parse_time
from .base import ProviderContext, SearchProvider


class _JSONProvider(SearchProvider):
    """JSON API 源公共逻辑。"""

    needs_key = True
    api_key = ""
    endpoint = ""

    def is_available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {}

    def _params(self, query: str, options: QueryOptions) -> dict:
        raise NotImplementedError

    def _parse(self, data: dict) -> List[SearchResult]:
        raise NotImplementedError

    def search(self, query: str, options: QueryOptions,
               ctx: ProviderContext) -> List[SearchResult]:
        if not self.api_key:
            raise UpstreamError(f"{self.label} 未配置 API Key", source=self.name)
        resp = ctx.http.get(self.endpoint,
                            params=self._params(query, options),
                            headers=self._headers(),
                            timeout=options.timeout, retries=options.retries,
                            source=self.name)
        if resp.status_code != 200:
            raise UpstreamError(f"{self.label} 返回 {resp.status_code}",
                                source=self.name, status_code=resp.status_code)
        import json
        try:
            data = json.loads(resp.text)
        except Exception as e:      # noqa: BLE001
            raise UpstreamError(f"{self.label} 响应解析失败: {e}", source=self.name)
        items = self._parse(data)
        if not items:
            return []
        return self._limit(items, options)


class TavilyProvider(_JSONProvider):
    name = "tavily"
    label = "Tavily"
    endpoint = "https://api.tavily.com/search"
    max_results = 20

    def __init__(self, config=None):
        super().__init__(config)
        self.api_key = self.cfg.tavily_key

    def _params(self, query: str, options: QueryOptions) -> dict:
        return {
            "api_key": self.api_key,
            "query": query,
            "max_results": min(max(options.top_k * 2, 10), 20),
            "search_depth": "basic",
            "include_answer": "false",
        }

    def _parse(self, data: dict) -> List[SearchResult]:
        out = []
        for it in data.get("results", []):
            dt = parse_time(it.get("published_date") or it.get("published_time"))
            r = self._mk(it.get("title", ""), it.get("url", ""),
                         it.get("content", ""), dt.isoformat() if dt else None,
                         self.name)
            if r:
                r.relevance_score = float(it.get("score") or 0.0)
                out.append(r)
        return out


class SerpAPIProvider(_JSONProvider):
    name = "serpapi"
    label = "SerpAPI"
    endpoint = "https://serpapi.com/search.json"
    max_results = 20

    def __init__(self, config=None):
        super().__init__(config)
        self.api_key = self.cfg.serpapi_key

    def _params(self, query: str, options: QueryOptions) -> dict:
        p = {"q": query, "api_key": self.api_key, "engine": "google",
             "num": min(max(options.top_k * 2, 10), 20)}
        if options.lang == "zh":
            p["hl"] = "zh-cn"
            p["gl"] = "cn"
        return p

    def _parse(self, data: dict) -> List[SearchResult]:
        out = []
        for it in data.get("organic_results", []):
            dt = parse_time(it.get("date"))
            r = self._mk(it.get("title", ""), it.get("link", ""),
                         it.get("snippet", ""), dt.isoformat() if dt else None,
                         self.name)
            if r:
                out.append(r)
        return out


class BraveProvider(_JSONProvider):
    name = "brave"
    label = "Brave Search"
    endpoint = "https://api.search.brave.com/res/v1/web/search"
    max_results = 20

    def __init__(self, config=None):
        super().__init__(config)
        self.api_key = self.cfg.brave_key

    def _headers(self) -> dict:
        return {"X-Subscription-Token": self.api_key,
                "Accept": "application/json"}

    def _params(self, query: str, options: QueryOptions) -> dict:
        p = {"q": query, "count": min(max(options.top_k * 2, 10), 20)}
        if options.freshness.value != "any":
            p["freshness"] = {"1d": "pd", "1w": "pw", "1m": "pm",
                              "1y": "py"}.get(options.freshness.value, "")
        return p

    def _parse(self, data: dict) -> List[SearchResult]:
        out = []
        for it in data.get("web", {}).get("results", []):
            dt = parse_time(it.get("page_age") or it.get("age"))
            r = self._mk(it.get("title", ""), it.get("url", ""),
                         it.get("description", ""),
                         dt.isoformat() if dt else None, self.name)
            if r:
                out.append(r)
        return out
