"""Bing HTML 检索源(cn.bing.com)。

作为 RSS 源的补充: 覆盖更多结果、可带站点归属与部分日期。
"""
from __future__ import annotations

from typing import List

from ..errors import UpstreamError
from ..extract.dom import parse_html
from ..models import QueryOptions, SearchResult
from .base import ProviderContext, SearchProvider
from .helpers import pick_date, pick_snippet, pick_title_url


class BingHTMLProvider(SearchProvider):
    name = "bing_html"
    label = "Bing 网页"
    max_results = 20

    ENDPOINT = "https://cn.bing.com/search"

    def search(self, query: str, options: QueryOptions,
               ctx: ProviderContext) -> List[SearchResult]:
        params = {"q": query, "count": "30", "setlang": "zh-CN"}
        if options.site:
            params["q"] = f"{query} site:{options.site}"
        resp = ctx.fetch(self.ENDPOINT, params=params,
                            timeout=options.timeout, retries=options.retries,
                            lang=options.lang, source=self.name)
        if resp.status_code != 200 or not resp.text:
            raise UpstreamError(f"Bing HTML 返回异常 status={resp.status_code}",
                                source=self.name, status_code=resp.status_code)
        root = parse_html(resp.text)
        out: List[SearchResult] = []
        blocks = root.find_all("li", "b_algo")
        if not blocks:
            blocks = root.find_all("div", "b_algo")
        for b in blocks:
            title, url = pick_title_url(b, self.ENDPOINT,
                                        title_tags=("h2",),
                                        title_classes=("b_algoHeader",))
            if not url:
                continue
            snip = pick_snippet(b, exclude_text=title,
                                classes=("b_lineclamp2", "b_lineclamp3",
                                         "b_caption", "b_paractl"))
            date = pick_date(b, snip)
            r = self._mk(title, url, snip, date, self.name)
            if r:
                out.append(r)
        return self._limit(out, options)
