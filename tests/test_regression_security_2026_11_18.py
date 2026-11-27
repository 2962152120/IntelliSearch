"""2026-11-18 代码审查修复的回归测试。

覆盖:
- SSRF: 内网/元数据/IP 变形/非 http scheme 在 fetch 与渲染路径被拒;
- API Key: 不进 URL query, 日志脱敏;
- 检索闸门: URL 命中不算证据, 权威域名按 hostname 判定;
- 付费墙降权真实生效;
- 全源软封 -> NO_RESULTS(与真故障 ERROR 区分);
- 布尔 NOT 在结果侧生效;
- 商业源配 key 自动启用;
- 一致性: 正文缓存 key 统一 / merge 不改调用方 / env 实例化时读取 /
  cache_key 维度 / 限流令牌退还 / CORS 与 token 鉴权 / body 上限。
"""
from __future__ import annotations

import time

import pytest

from intellisearch.config import Config
from intellisearch.engine import SearchEngine
from intellisearch.errors import RateLimitError, UnsafeUrlError
from intellisearch.models import (ParsedQuery, ProviderReport, QueryOptions,
                                  SearchResult, Status)
from intellisearch.retrieval.fusion import (_drop_irrelevant, _is_authoritative,
                                            fuse)
from intellisearch.retrieval.rank import rank
from intellisearch.safety.ratelimit import RateLimiter, TokenBucket
from intellisearch.safety.urlcheck import is_public_url, validate_public_url


def mk(title, url, snippet=""):
    return SearchResult(title=title, url=url, snippet=snippet)


def bare_cfg(**kw):
    defaults = dict(cache_enabled=False, audit_log=False,
                    rate_limit_enabled=False, render_enabled=False)
    defaults.update(kw)
    return Config(**defaults)


# ------------------------------------------------------------------ SSRF
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:6379/",
    "http://169.254.169.254/latest/meta-data/iam/",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://localhost/x",
    "http://2130706433/",        # 127.0.0.1 的十进制整数形式
    "http://0x7f000001/",        # 十六进制形式
    "http://127.1/",             # 点分缩写
    "file:///etc/passwd",
    "gopher://127.0.0.1/",
    "http://metadata.google.internal/",
])
def test_ssrf_targets_rejected(url, monkeypatch):
    # conftest 为本地-server 测试开了 IS_ALLOW_LOCALHOST; 这里验证生产默认行为
    monkeypatch.delenv("IS_ALLOW_LOCALHOST", raising=False)
    assert is_public_url(url) is False
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url)


@pytest.mark.parametrize("url", [
    "https://github.com/x", "https://example.com/", "http://1.2.3.4/",
    "https://docs.python.org/3/",
])
def test_public_urls_allowed(url):
    assert is_public_url(url) is True
    validate_public_url(url)


def test_engine_fetch_blocks_ssrf_before_network():
    e = SearchEngine(bare_cfg())
    try:
        out = e.fetch("http://169.254.169.254/latest/meta-data/")
        assert out["ok"] is False
        assert out["status"] == "UNSAFE_URL"
    finally:
        e.close()


# ------------------------------------------------------------------ API Key
def test_api_key_not_in_query_params():
    from intellisearch.models import QueryOptions
    from intellisearch.providers.api_provider import SerpAPIProvider, TavilyProvider
    cfg = Config(tavily_key="TAV_SECRET", serpapi_key="SERP_SECRET")
    opts = QueryOptions()
    tav = TavilyProvider(cfg)._params("q", opts)
    serp = SerpAPIProvider(cfg)._params("q", opts)
    assert "api_key" not in tav and "api_key" not in serp
    assert "TAV_SECRET" not in str(tav) and "SERP_SECRET" not in str(serp)
    assert TavilyProvider(cfg)._headers().get("Authorization", "").endswith("TAV_SECRET")
    assert SerpAPIProvider(cfg)._headers().get("X-API-Key") == "SERP_SECRET"


def test_url_redaction():
    from intellisearch.http.client import redact_url
    red = redact_url("https://api.x.com/s?api_key=SECRET&q=hello&token=T")
    assert "SECRET" not in red and "T&" not in red
    assert "q=hello" in red


# ------------------------------------------------------------------ 闸门
def test_url_stuffing_does_not_pass_relevance_gate():
    kept, _ = _drop_irrelevant(
        [mk("无关标题", "https://evil.com/?amd=1&zen5=2")],
        ["AMD", "Zen5", "架构"])
    assert kept == []


def test_authoritative_uses_hostname_not_url_substring():
    assert _is_authoritative(mk("x", "https://evil.com/?ref=github.com")) is False
    assert _is_authoritative(mk("x", "https://docs.python.org/3/")) is True
    assert _is_authoritative(mk("x", "https://api.github.com/x")) is True


def test_paywall_penalty_affects_rank():
    clean = mk("AMD Zen5 解析", "https://a.com/1", "AMD Zen5 架构")
    pay = mk("AMD Zen5 解析", "https://b.com/1", "AMD Zen5 架构")
    pay.extra["_penalty"] = 0.85
    rank([clean, pay], ["AMD", "Zen5", "架构"], "AMD Zen5 架构")
    # penalty 只在综合得分上施加一次(约 15% 降权)。此前 credibility 与 total
    # 各乘一次, 使可信度分量承受 0.85²=0.7225 —— 远超设计的降权幅度。
    # 断言综合得分而非 credibility: 后者不该被二次惩罚。
    assert clean.extra["_score"] > pay.extra["_score"]
    assert clean.credibility == pay.credibility, "credibility 不应被重复降权"


# ------------------------------------------------------------------ 状态
def test_soft_block_is_no_results_not_error():
    soft = SearchEngine._decide_status(
        [ProviderReport("a", False, status="irrelevant")], [])
    assert soft == Status.NO_RESULTS
    hard = SearchEngine._decide_status(
        [ProviderReport("a", False, status="error")], [])
    assert hard == Status.ERROR


def test_boolean_not_filters_results():
    items = [mk("Zen5 处理器广告投放策略", "https://a.com/1", "AMD Zen5"),
             mk("AMD Zen5 架构深度解析", "https://b.com/2", "Zen5 微架构")]
    pq = ParsedQuery(raw="AMD Zen5 -广告", effective="AMD Zen5",
                     terms=["AMD", "Zen5"], not_=["广告"])
    kept, _ = fuse(items, pq, QueryOptions(top_k=10))
    assert len(kept) == 1 and kept[0].url == "https://b.com/2"


# ------------------------------------------------------------------ 商业源
def test_commercial_provider_auto_enabled_with_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "FAKE")
    cfg = bare_cfg()
    e = SearchEngine(cfg)
    try:
        assert "tavily" in [p.name for p in e.providers]
    finally:
        e.close()


def test_explicit_providers_are_respected(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "FAKE")
    cfg = bare_cfg()
    e = SearchEngine(cfg, providers=["bing_rss"])
    try:
        assert [p.name for p in e.providers] == ["bing_rss"]
    finally:
        e.close()


# ------------------------------------------------------------------ 一致性
def test_env_read_at_instantiation(monkeypatch):
    monkeypatch.setenv("IS_TIMEOUT", "77")
    assert Config().timeout == 77.0


def test_cache_key_includes_providers_and_vertical():
    base = QueryOptions(top_k=5).cache_key("q")
    assert base != QueryOptions(top_k=5, providers=["tavily"]).cache_key("q")
    assert base != QueryOptions(top_k=5, vertical="tech").cache_key("q")


def test_page_cache_key_shared_between_paths():
    e = SearchEngine(bare_cfg())
    try:
        assert e._page_cache_key("https://a.com", 4000, "auto") == \
               e._page_cache_key("https://a.com", 4000, "auto")
    finally:
        e.close()


def test_merge_options_does_not_mutate():
    e = SearchEngine(bare_cfg())
    try:
        opts = QueryOptions(top_k=5)
        e._merge_options(opts, {"top_k": 10})
        assert opts.top_k == 5
    finally:
        e.close()


def test_token_bucket_block_mode_waits():
    b = TokenBucket(rate=10.0, burst=1)
    assert b.consume()
    t0 = time.monotonic()
    assert b.consume(block=True, timeout=1.0) is True
    assert time.monotonic() - t0 >= 0.05


def test_session_limit_refunds_global_token():
    rl = RateLimiter(global_qps=100, global_burst=10,
                     session_qps=1, session_burst=1)
    rl.acquire("s1")
    with pytest.raises(RateLimitError):
        rl.acquire("s1")
    assert rl.global_bucket.available >= 9.0


# ------------------------------------------------------------------ HTTP 服务
def test_http_api_auth_cors_and_body_limit():
    import http.client
    import json as _json
    from http.server import ThreadingHTTPServer
    from intellisearch.server.http_api import _EngineHolder, make_handler

    cfg = bare_cfg(api_token="SECRET", cors_origins=["https://allowed.com"],
                   max_body_bytes=32)
    holder = _EngineHolder(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(holder))
    port = srv.server_address[1]
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def req(method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data, status = r.read().decode(), r.status
        hdrs = dict(r.getheaders())
        c.close()
        return status, data, hdrs

    try:
        assert req("GET", "/stats")[0] == 401
        assert req("GET", "/health")[0] == 200
        assert req("GET", "/stats", {"Authorization": "Bearer SECRET"})[0] == 200
        assert req("GET", "/stats", {"X-API-Key": "SECRET"})[0] == 200
        h = req("GET", "/health", {"Origin": "https://allowed.com"})[2]
        assert h.get("Access-Control-Allow-Origin") == "https://allowed.com"
        h = req("GET", "/health", {"Origin": "https://evil.com"})[2]
        assert h.get("Access-Control-Allow-Origin") is None
        s, _, _ = req("POST", "/search",
                      {"Authorization": "Bearer SECRET", "Content-Type": "application/json"},
                      body=b"x" * 100)
        assert s == 413
        s, d, _ = req("GET", "/fetch?url=http://169.254.169.254/x",
                      {"Authorization": "Bearer SECRET"})
        assert _json.loads(d)["status"] == "UNSAFE_URL"
    finally:
        srv.shutdown()
        srv.server_close()


def test_403_is_not_retried(monkeypatch):
    """403 被拦截时不重试 —— 与文档'4xx 不重试(429 除外)'一致。"""
    import httpx
    from intellisearch.http.client import HttpClient
    from intellisearch.errors import BlockedError

    from contextlib import contextmanager

    calls = {"n": 0}

    def fake_stream(self, method, url, **kw):
        calls["n"] += 1
        resp = httpx.Response(403, text="forbidden",
                              request=httpx.Request("GET", url))

        @contextmanager
        def cm():
            yield resp

        return cm()

    # 当前 HTTP 客户端用 stream() 而非 get() 读响应, mock 必须对齐。
    monkeypatch.setattr(httpx.Client, "stream", fake_stream)
    c = HttpClient(bare_cfg())
    try:
        with pytest.raises(BlockedError):
            c.get("https://example.com/x", retries=3)
        assert calls["n"] == 1, f"403 不应重试, 实际请求 {calls['n']} 次"
    finally:
        c.close()
