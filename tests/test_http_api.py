"""HTTP REST 服务(离线)测试: 状态码映射 / providers 透传。

全部打本机 127.0.0.1 随机端口(端口 0) 起 ThreadingHTTPServer, 不依赖外网;
每个用例在 finally 里 shutdown + server_close, 不泄漏线程/端口。
"""
from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from intellisearch.models import (ProviderReport, QueryOptions, SearchResponse,
                                  SearchResult, Status)
from intellisearch.server.http_api import make_handler


def _resp(status: Status) -> SearchResponse:
    """构造指定状态、字段齐全的响应。"""
    has_results = status in (Status.OK, Status.PARTIAL)
    results = ([SearchResult(title="示例标题", url="https://example.com/1",
                             snippet="示例摘要", source="s1")]
               if has_results else [])
    return SearchResponse(
        query="x",
        status=status,
        results=results,
        providers=[ProviderReport(name="s1", ok=has_results,
                                  status="ok" if has_results else "error",
                                  count=len(results),
                                  error="" if has_results else "模拟源故障")],
        total_found=len(results),
        message=f"状态 {status.value}",
    )


class FakeEngine:
    """记录调用参数的引擎替身。"""

    def __init__(self, response_getter):
        self._response_getter = response_getter
        self.calls = []

    def search(self, query, options):
        self.calls.append((query, options))
        return self._response_getter()

    def stats(self):
        return {}


class FakeHolder:
    """替代 _EngineHolder: 直接返回固定引擎, 不触发真实 SearchEngine 构造。"""

    def __init__(self, engine):
        self.engine = engine

    def get(self):
        return self.engine


@contextmanager
def _serve(engine):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                make_handler(FakeHolder(engine)))
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, name="is-http-api-test",
                          daemon=True)
    th.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)


def _request(port: int, method: str, path: str, body=None):
    """发一个请求, 返回 (status, text)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        if body is None:
            conn.request(method, path)
        else:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            conn.request(method, path, body=raw,
                         headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8")
    finally:
        conn.close()


@pytest.mark.parametrize("status,expected_code,expected_ok", [
    (Status.OK, 200, True),
    (Status.PARTIAL, 200, True),
    (Status.NO_RESULTS, 200, False),      # 正常业务状态, 修复前是 502
    (Status.BLOCKED, 403, False),
    (Status.ERROR, 502, False),
])
def test_get_search_maps_status_to_http_code(status, expected_code, expected_ok):
    engine = FakeEngine(lambda: _resp(status))
    with _serve(engine) as port:
        code, body = _request(port, "GET", "/search?" + urlencode({"q": "x"}))
    data = json.loads(body)
    assert code == expected_code
    assert data["status"] == status.value
    assert data["ok"] is expected_ok


def test_get_search_passes_providers_to_engine():
    """GET ?providers=s2 必须解析并透传到 QueryOptions。"""
    engine = FakeEngine(lambda: _resp(Status.OK))
    with _serve(engine) as port:
        code, body = _request(
            port, "GET", "/search?" + urlencode({"q": "x", "providers": "s2"}))
    assert code == 200
    assert engine.calls, "引擎必须被调用"
    query, opts = engine.calls[-1]
    assert query == "x"
    assert isinstance(opts, QueryOptions)
    assert opts.providers == ["s2"]


def test_get_search_passes_multiple_providers():
    engine = FakeEngine(lambda: _resp(Status.OK))
    with _serve(engine) as port:
        _request(port, "GET",
                 "/search?" + urlencode({"q": "x", "providers": "s1,s2"}))
    assert engine.calls[-1][1].providers == ["s1", "s2"]


def test_post_search_ok_returns_200_with_results():
    engine = FakeEngine(lambda: _resp(Status.OK))
    with _serve(engine) as port:
        code, body = _request(port, "POST", "/search",
                              {"query": "x", "top_k": 3})
    data = json.loads(body)
    assert code == 200
    assert data["status"] == "ok" and data["ok"] is True
    assert data["results"] and data["results"][0]["title"] == "示例标题"
    assert engine.calls[-1][0] == "x"
    assert engine.calls[-1][1].top_k == 3


def test_post_search_no_results_is_200_not_502():
    """POST /search 的 no_results 同样是 200, 不能当服务故障。"""
    engine = FakeEngine(lambda: _resp(Status.NO_RESULTS))
    with _serve(engine) as port:
        code, body = _request(port, "POST", "/search", {"query": "x"})
    data = json.loads(body)
    assert code == 200
    assert data["status"] == "no_results" and data["ok"] is False
    assert data["results"] == []


@pytest.mark.parametrize("fmt", ["context", "markdown"])
def test_text_formats_share_status_code_mapping(fmt):
    """context/markdown 输出与 JSON 分支共用状态码映射(口径不能分裂)。

    修复前这两个分支恒 200: 上游全挂(error)时调用方只看到 200 + 一段
    "未找到"文本, 与 JSON 分支的 502 自相矛盾。
    """
    engine = FakeEngine(lambda: _resp(Status.ERROR))
    with _serve(engine) as port:
        code, body = _request(
            port, "GET", "/search?" + urlencode({"q": "x", "format": fmt}))
    assert code == 502, f"{fmt} 分支的 error 状态应映射为 502, 实际 {code}"
    assert body.strip(), f"{fmt} 分支仍应返回可读文本"

    engine = FakeEngine(lambda: _resp(Status.NO_RESULTS))
    with _serve(engine) as port:
        code, body = _request(
            port, "GET", "/search?" + urlencode({"q": "x", "format": fmt}))
    assert code == 200, f"{fmt} 分支的 no_results 是正常业务状态, 应为 200"
