"""MCP 服务的工具实现测试: context / json 格式都只能检索一次。"""
from __future__ import annotations

import json

from intellisearch.models import (ProviderReport, QueryOptions, SearchResponse,
                                  SearchResult, Status)
from intellisearch.server.mcp_server import MCPServer


def _resp() -> SearchResponse:
    return SearchResponse(
        query="检索词",
        status=Status.OK,
        results=[SearchResult(title="示例标题", url="https://example.com/1",
                              snippet="简短摘要", clean_text="正文关键内容ABC",
                              source="s1")],
        providers=[ProviderReport(name="s1", ok=True, status="ok", count=1)],
        total_found=1,
    )


class FakeEngine:
    """记录检索次数的引擎替身。

    search_context 故意做成"内部再 search 一次"(与真实引擎一致): 若
    _tool_search 仍走旧实现, 这个替身会把上层的二次检索如实暴露出来。
    """

    def __init__(self):
        self.search_calls = []
        self.context_calls = 0

    def search(self, query, options):
        self.search_calls.append((query, options))
        return _resp()

    def search_context(self, query, options=None, max_chars=6000, **kw):
        self.context_calls += 1
        resp = self.search(query, options)
        return resp.to_context(
            max_chars=max_chars,
            with_content=bool(options and options.fetch_content))


def _server_with(engine: FakeEngine) -> MCPServer:
    """绕过 __init__ 里的真实引擎构造, 只注入替身。"""
    srv = MCPServer.__new__(MCPServer)
    srv.engine = engine
    return srv


def test_context_format_searches_only_once():
    """format=context: 整轮检索只能跑一次(修复前 search 被调用 2 次)。"""
    engine = FakeEngine()
    srv = _server_with(engine)
    out = srv._tool_search({"query": "检索词", "format": "context",
                            "max_chars": 2000, "fetch_content": True})
    assert len(engine.search_calls) == 1, "context 格式不应二次检索"
    assert engine.context_calls == 0, "不应再走会重复检索的 search_context"
    # 渲染口径与 HTTP API 一致: 带编号引用 + 正文(开启了 fetch_content)
    assert "[1]" in out
    assert "示例标题" in out
    assert "https://example.com/1" in out
    assert "正文关键内容ABC" in out
    # 检索参数透传: max_chars 只影响输出, fetch_content 进入 QueryOptions
    assert engine.search_calls[0][0] == "检索词"
    assert isinstance(engine.search_calls[0][1], QueryOptions)
    assert engine.search_calls[0][1].fetch_content is True


def test_json_format_searches_only_once():
    engine = FakeEngine()
    srv = _server_with(engine)
    out = srv._tool_search({"query": "检索词", "format": "json",
                            "max_chars": 4000})
    assert len(engine.search_calls) == 1, "json 格式不应额外检索"
    assert engine.context_calls == 0
    data = json.loads(out)
    assert data["status"] == "ok" and data["ok"] is True
    assert data["results"][0]["title"] == "示例标题"
    assert data["results"][0]["url"] == "https://example.com/1"
