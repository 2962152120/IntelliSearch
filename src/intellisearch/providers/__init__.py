"""检索源注册表。

新增源: 在此登记 name -> 类, 并在 config.providers 中加入 name 即可启用。
"""
from .base import SearchProvider, ProviderContext
from .bing_rss import BingRSSProvider
from .bing_html import BingHTMLProvider
from .sogou import SogouProvider
from .so360 import So360Provider
from .api_provider import TavilyProvider, SerpAPIProvider, BraveProvider

REGISTRY = {
    "bing_rss": BingRSSProvider,
    "bing_html": BingHTMLProvider,
    "sogou": SogouProvider,
    "so360": So360Provider,
    "tavily": TavilyProvider,
    "serpapi": SerpAPIProvider,
    "brave": BraveProvider,
}

# 默认启用顺序: 结构化源优先, 其后为补充源
DEFAULT_PROVIDERS = ["bing_rss", "bing_html", "sogou", "so360"]

__all__ = ["REGISTRY", "DEFAULT_PROVIDERS", "SearchProvider", "ProviderContext",
           "BingRSSProvider", "BingHTMLProvider", "SogouProvider", "So360Provider",
           "TavilyProvider", "SerpAPIProvider", "BraveProvider"]
