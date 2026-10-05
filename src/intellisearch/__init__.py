"""IntelliSearch - 面向大模型的自研联网检索工具包。

统一入口::

    from intellisearch import SearchEngine
    engine = SearchEngine()
    resp = engine.search("铭凡 UM880 Pro 的 NPU 算力多少", top_k=5)
    print(resp.to_json())

本模块采用惰性导入, 只 import 包本身不会触发重量级依赖加载。
"""
__version__ = "1.0.0"

from .models import (SearchResult, SearchResponse, QueryOptions, SourceType,
                     Status, Freshness, ParsedQuery, ProviderReport)

__all__ = [
    "SearchEngine",
    "SearchResult", "SearchResponse", "QueryOptions",
    "SourceType", "Status", "Freshness", "ParsedQuery", "ProviderReport",
    "__version__",
]


def __getattr__(name: str):
    """惰性导入 SearchEngine, 避免 import 包时加载全部依赖。"""
    if name == "SearchEngine":
        from .engine import SearchEngine
        return SearchEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(list(globals().keys()) + __all__))
