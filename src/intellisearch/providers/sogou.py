"""搜狗检索源。

特点: 中文覆盖好、结果带日期; 链接为 /link?url=xxx 跳转形式, 需还原。
"""
from __future__ import annotations

from typing import List

from ..errors import UpstreamError
from ..extract.dom import parse_html
from ..models import QueryOptions, SearchResult
from .base import ProviderContext, SearchProvider
from .helpers import pick_date, pick_snippet, pick_title_url, resolve_redirects


class SogouProvider(SearchProvider):
    name = "sogou"
    label = "搜狗"
    max_results = 20

    ENDPOINT = "https://www.sogou.com/web"

    def search(self, query: str, options: QueryOptions,
               ctx: ProviderContext) -> List[SearchResult]:
        params = {"query": query}
        if options.site:
            params["query"] = f"{query} site:{options.site}"
        resp = ctx.fetch(self.ENDPOINT, params=params,
                            timeout=options.timeout, retries=options.retries,
                            lang=options.lang, source=self.name)
        if resp.status_code != 200 or not resp.text:
            raise UpstreamError(f"搜狗返回异常 status={resp.status_code}",
                                source=self.name, status_code=resp.status_code)
        root = parse_html(resp.text)
        out: List[SearchResult] = []
        blocks = root.find_all("div", "vrwrap")
        if not blocks:
            blocks = root.find_all("div", "rb")
        for b in blocks:
            title, url = pick_title_url(
                b, "https://www.sogou.com",
                title_tags=("h3",),
                title_classes=("vr-title",),
                allow_redirect_links=True)
            if not url:
                continue
            snip = pick_snippet(b, exclude_text=title,
                                classes=("space-txt", "text-layout", "fz-mid"))
            if not snip:
                continue
            date = pick_date(b, snip)
            r = self._mk(title, url, snip, date, self.name)
            if r:
                out.append(r)
        out = self._limit(out, options)
        return resolve_redirects(out, ctx)
