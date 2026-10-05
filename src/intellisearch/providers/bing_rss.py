"""Bing RSS 检索源 —— 结构化程度最高, 作为主源。

优点: 返回标准 RSS, 含 title/link/description/pubDate, 无需解析复杂 HTML。
缺点: 单次固定约 10 条。
"""
from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import quote_plus

from ..errors import UpstreamError
from ..models import QueryOptions, SearchResult, parse_time
from .base import ProviderContext, SearchProvider


class BingRSSProvider(SearchProvider):
    name = "bing_rss"
    label = "Bing RSS"
    max_results = 10

    ENDPOINT = "https://www.bing.com/search"

    def search(self, query: str, options: QueryOptions,
               ctx: ProviderContext) -> List[SearchResult]:
        params = {"q": query, "format": "rss", "count": "30"}
        if options.site:
            params["q"] = f"{query} site:{options.site}"
        resp = ctx.fetch(self.ENDPOINT, params=params,
                            timeout=options.timeout, retries=options.retries,
                            lang=options.lang, source=self.name)
        if resp.status_code != 200 or not resp.text:
            raise UpstreamError(f"Bing RSS 返回异常 status={resp.status_code}",
                                source=self.name, status_code=resp.status_code)
        items = self._parse(resp.text)
        if not items:
            return []
        return self._limit(items, options)

    def _parse(self, xml: str) -> List[SearchResult]:
        out: List[SearchResult] = []
        blocks = re.findall(r"<item>(.*?)</item>", xml, re.S | re.I)
        for b in blocks:
            title = self._field(b, "title")
            link = self._field(b, "link")
            desc = self._field(b, "description")
            pub = self._field(b, "pubDate")
            if not link:
                m = re.search(r"href=[\"'](https?://[^\"']+)", b, re.I)
                link = m.group(1) if m else ""
            dt = parse_time(pub)
            r = self._mk(self._clean(title), link, self._clean(desc),
                         dt.isoformat() if dt else None, self.name)
            if r:
                out.append(r)
        return out

    @staticmethod
    def _field(block: str, name: str) -> str:
        m = re.search(rf"<{name}>(.*?)</{name}>", block, re.S | re.I)
        if not m:
            return ""
        v = m.group(1)
        v = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", v, flags=re.S)
        return v.strip()

    @staticmethod
    def _clean(s: str) -> str:
        import html as _h
        s = re.sub(r"<[^>]+>", "", s or "")
        s = _h.unescape(s)
        return re.sub(r"\s+", " ", s).strip()
