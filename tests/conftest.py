"""测试公共夹具。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SERP_DIR = FIXTURES / "serp"


def pytest_addoption(parser):
    parser.addoption("--run-integration", action="store_true", default=False,
                     help="运行需要真实网络的集成测试")


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: 需要真实网络的测试")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="需要 --run-integration 才会执行(依赖真实网络)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def load_serp(name: str) -> str:
    """加载保存的搜索结果页样本, 用于离线解析测试。"""
    p = SERP_DIR / name
    return p.read_text(encoding="utf-8")


def load_page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttp:
    """可控的 HTTP 客户端替身。

    responses: url 子串 -> (status_code, text) 或 Exception
    """

    def __init__(self, default_text: str = "", default_status: int = 200,
                 responses: dict = None, final_url: str = "https://example.com/x"):
        self.default_text = default_text
        self.default_status = default_status
        self.responses = responses or {}
        self.final_url = final_url
        self.calls = []
        # 记录每次调用收到的关键字参数 —— 检索源靠 params 传查询串,
        # 曾有改动漏传 params 导致四个源全部 TypeError 而测试仍全绿。
        self.kwargs = []

    def get(self, url, **kw):
        from intellisearch.http.client import FetchResponse
        self.calls.append(url)
        self.kwargs.append(kw)
        for key, val in self.responses.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                if isinstance(val, FetchResponse):
                    return val
                status, text = val
                return FetchResponse(url=self.final_url, status_code=status, text=text)
        if isinstance(self.default_text, Exception):
            raise self.default_text
        return FetchResponse(url=self.final_url, status_code=self.default_status,
                             text=self.default_text)

    def close(self):
        pass


class FailingProvider:
    """永远失败的检索源, 用于验证降级与状态上报。"""

    name = "failing"

    def __init__(self, error=None):
        from intellisearch.errors import UpstreamError
        self._error = error or UpstreamError("模拟源故障", source="failing")
        self.is_available = lambda: True

    def is_available(self):
        return True

    def search(self, query, options, ctx):
        raise self._error


class StaticProvider:
    """返回固定结果的检索源。"""

    def __init__(self, name="static", results=None):
        self.name = name
        self._results = results or []
        self.calls = []

    def is_available(self):
        return True

    def search(self, query, options, ctx):
        self.calls.append(query)
        return list(self._results)
