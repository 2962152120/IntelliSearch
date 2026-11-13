"""测试公共夹具。"""
from __future__ import annotations

import http.server
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SERP_DIR = FIXTURES / "serp"

# ---------------------------------------------------------------- 本机测试站点
# 渲染/抓取类集成用例一律打本机 HTTP 服务, 不依赖 example.com / httpbin 等
# 外部站点。2026-10-31 的 CI 集成失败就是外部 example.com 对 GitHub runner
# 返回 Cloudflare 503 引起的 —— 渲染链路本身正常, 测试却红了。
STATIC_PAGE_MARKER = "local-static-marker"
SPA_JS_MARKER = "app-ready"

STATIC_PAGE = (
    "<!doctype html><html><head><title>Local Static</title></head><body>"
    f"<h1>{STATIC_PAGE_MARKER}</h1>"
    + "<p>这是一段有实际内容的正文段落，用来模拟普通静态文档页。</p>" * 20
    + "</body></html>"
)

# 短小的 SPA 骨架页: 命中 id="app" 挂载点, 正文由 JS 注入
SPA_PAGE = (
    "<!doctype html><html><body>"
    '<div id="app">loading</div>'
    f"<script>document.getElementById('app').textContent='{SPA_JS_MARKER}';</script>"
    "</body></html>"
)

# 本机站点收到的请求路径(含图片等子资源)。集成用例据此硬断言"资源真的被拦/
# 真的被加载", 比只看渲染器自报的 blocked_requests 更难造假。
# 全局列表会跨用例累积, 用到它的用例应先 clear() 再断言。
LOCAL_SITE_HITS: list = []

# /pics 引用的 1x1 透明 PNG(合法图片, Chromium 会真的发出请求)
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

# 带两张图片的页面: 用于验证 block_resources 开关是否真的接线
PICS_PAGE = (
    "<!doctype html><html><body><h1>pics</h1>"
    '<img src="/img.png?n=1"><img src="/img.png?n=2">'
    "</body></html>"
)

# 渲染执行 JS 后才把 navigator.language 写进正文 —— 验证 lang/locale 真的生效
LANG_PAGE = (
    "<!doctype html><html><body><div>pending</div>"
    "<script>document.body.textContent=navigator.language;</script>"
    "</body></html>"
)


class _LocalSiteHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        LOCAL_SITE_HITS.append(path)        # 每个请求都留痕(含子资源)
        if path == "/slow":
            # 确定性的慢响应: 渲染超时用例不必再赌 httpbin 的跨境网络
            time.sleep(8)
        content_type = "text/html; charset=utf-8"
        if path == "/spa":
            body = SPA_PAGE.encode("utf-8")
        elif path == "/pics":
            body = PICS_PAGE.encode("utf-8")
        elif path == "/lang":
            body = LANG_PAGE.encode("utf-8")
        elif path == "/img.png":
            body = PNG_1X1
            content_type = "image/png"
        else:
            body = STATIC_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):       # 静音访问日志
        pass


@pytest.fixture(scope="session")
def local_site():
    """本机 HTTP 服务, 返回 base URL; 会话结束时关闭。"""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LocalSiteHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, name="is-test-site",
                     daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()



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
