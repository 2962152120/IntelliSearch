"""360 搜索(so.com)检索源。

特点: 与搜狗独立索引, 结果带日期; 链接为 so.com/link?m=xxx 跳转形式。
"""
from __future__ import annotations

from typing import List

from ..errors import UpstreamError
from ..extract.dom import parse_html
from ..models import QueryOptions, SearchResult
from .base import ProviderContext, SearchProvider
from .helpers import pick_date, pick_snippet, pick_title_url, resolve_redirects


class So360Provider(SearchProvider):
    name = "so360"
    label = "360搜索"
    max_results = 20

    ENDPOINT = "https://www.so.com/s"

    def search(self, query: str, options: QueryOptions,
               ctx: ProviderContext) -> List[SearchResult]:
        params = {"q": query}
        if options.site:
            params["q"] = f"{query} site:{options.site}"
        resp = ctx.fetch(self.ENDPOINT, params=params,
                            timeout=options.timeout, retries=options.retries,
                            lang=options.lang, source=self.name)
        if resp.status_code != 200 or not resp.text:
            raise UpstreamError(f"360搜索返回异常 status={resp.status_code}",
                                source=self.name, status_code=resp.status_code)
        root = parse_html(resp.text)
        out: List[SearchResult] = []
        blocks = root.find_all("li", "res-list")
        if not blocks:
            blocks = root.find_all("div", "res-list")
        for b in blocks:
            title, url = pick_title_url(
                b, "https://www.so.com",
                title_tags=("h3",),
                title_classes=("title",),
                allow_redirect_links=True)
            if not url:
                continue
            snip = pick_snippet(b, exclude_text=title,
                                classes=("res-desc", "mh-txt", "g-ellipsis2"))
            if not snip or len(snip) < 15:
                continue
            date = pick_date(b, snip)
            r = self._mk(title, url, snip, date, self.name)
            if r:
                out.append(r)
        out = self._limit(out, options)
        return resolve_redirects(out, ctx)
