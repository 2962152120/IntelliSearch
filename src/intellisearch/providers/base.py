"""检索源抽象。

新增一个源只需实现 SearchProvider 并在 config.providers 中登记名字。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..config import Config, DEFAULT_CONFIG
from ..models import QueryOptions, SearchResult
from ..http.client import HttpClient
from ..http.fetcher import SmartFetcher
from ..http.robots import RobotsCache


@dataclass
class ProviderContext:
    """一次检索在多个源之间共享的执行上下文。

    fetcher: SmartFetcher(带浏览器渲染升级能力)。
    检索源优先用 fetcher 而非 http, 这样某个源被反爬拦截时
    可以自动升级为浏览器渲染, 而不是直接判定该源失败。
    """

    http: HttpClient
    config: Config = field(default_factory=lambda: DEFAULT_CONFIG)
    robots: Optional[RobotsCache] = None
    lang: str = "zh"
    fetcher: Optional["SmartFetcher"] = None
    allow_render: bool = True

    def ensure_robots(self) -> RobotsCache:
        if self.robots is None:
            self.robots = RobotsCache(
                fetcher=lambda u: self.http.get(u, timeout=5, retries=0),
                user_agent="*")
        return self.robots

    def fetch(self, url: str, **kw):
        """统一抓取入口: 有渲染能力时走双模, 否则退回纯 HTTP。"""
        kw.setdefault("lang", self.lang)
        if self.fetcher is not None and self.allow_render:
            # rescue_only=True: 只有被拦截/空页才启用浏览器。
            # 搜索结果页脚本多、可见文字少, 若按"文本密度低"升级渲染,
            # 浏览器重渲染后的 DOM 结构会变(Bing 渲染后 b_algo 归零),
            # 反而把能正常解析的源变成 0 条, 且每次多花数秒。
            return self.fetcher.fetch(url, mode="auto", rescue_only=True, **kw)
        return self.http.get(url, **kw)


class SearchProvider:
    """检索源基类。search() 失败时抛 IntelliSearchError, 由引擎收敛。"""

    name: str = "base"
    label: str = "base"
    needs_key: bool = False
    max_results: int = 20

    def __init__(self, config: Config = None):
        self.cfg = config or DEFAULT_CONFIG

    def is_available(self) -> bool:
        """是否可参与本次检索(例如缺 API Key 时返回 False)。"""
        return True

    def search(self, query: str, options: QueryOptions,
               ctx: ProviderContext) -> List[SearchResult]:
        raise NotImplementedError

    # ---------- 公共工具 ----------
    @staticmethod
    def _mk(title: str, url: str, snippet: str = "", publish_time=None,
            source: str = "") -> Optional[SearchResult]:
        from ..models import SearchResult
        if not url or not url.startswith(("http://", "https://")):
            return None
        title = (title or "").strip()
        if not title:
            return None
        return SearchResult(title=title, url=url, snippet=(snippet or "").strip(),
                            publish_time=publish_time, source=source)

    def _limit(self, items: List[SearchResult], options: QueryOptions) -> List[SearchResult]:
        """源内截断: 取 top_k 的 2 倍, 给后续融合留余量。"""
        cap = min(self.max_results, max(options.top_k * 2, options.top_k + 5))
        return items[:cap]
