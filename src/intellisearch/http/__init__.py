"""HTTP 层：轻量抓取 + 无头浏览器渲染 + robots + UA 指纹。"""
from .client import HttpClient, FetchResponse
from .ua import UAPool
from .robots import RobotsCache, RobotsRule
from .browser import (BrowserPool, BrowserUnavailable, RenderError,
                      RenderRequest, RenderResult, find_chromium, get_pool)
from .fetcher import FetchOutcome, SmartFetcher

__all__ = [
    "HttpClient", "FetchResponse", "UAPool", "RobotsCache", "RobotsRule",
    "BrowserPool", "BrowserUnavailable", "RenderError", "RenderRequest",
    "RenderResult", "find_chromium", "get_pool",
    "FetchOutcome", "SmartFetcher",
]
