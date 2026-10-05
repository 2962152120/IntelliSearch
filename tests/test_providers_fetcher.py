"""检索源 × SmartFetcher 的联合测试(离线)。

背景: 离线测试里 providers 一直是 `ProviderContext(http=FakeHttp())`,
**从不带 fetcher**, 于是永远走 `http.get` 分支 —— 生产路径
(`fetcher.fetch`) 没有任何覆盖。后果是两类真实故障都能在全绿的情况下溜走:

- `SmartFetcher.fetch()` 少了 `params` 形参 → 四个源全部 TypeError;
- 启发式判定 "文本密度低" 把搜索结果页升级成浏览器渲染, 而重渲染后的 DOM
  结构变了(Bing 渲染后 `b_algo` 归零), 一个能正常解析的源被渲染成 0 条。

本文件用**真实的 SmartFetcher** + 假浏览器池来覆盖这条路径。
"""
import pytest

from intellisearch.config import Config
from intellisearch.http import SmartFetcher
from intellisearch.http.browser import RenderResult
from intellisearch.models import QueryOptions
from intellisearch.providers import REGISTRY
from intellisearch.providers.base import ProviderContext

from conftest import FakeHttp, load_serp


class FakePool:
    """假浏览器池: 记录调用, 并特意返回一份**结构不同**的 HTML。

    "结构不同"是关键 —— 真实浏览器渲染出的 SERP 与原始 HTML 并不是同一棵树,
    因此只要检索源被升级到渲染, 解析结果必然归零。测试正是靠这一点暴露问题。
    """

    def __init__(self, html="<html><body>rendered</body></html>", status=200):
        self.html = html
        self.status = status
        self.calls = []

    def available(self):
        return True

    def engine_name(self):
        return "fake-chromium"

    def render(self, req):
        self.calls.append(req.url)
        return RenderResult(url=req.url, final_url=req.url, status=self.status,
                            html=self.html, text="rendered", title="",
                            engine="fake-chromium")

    def close(self, timeout=None):
        pass


def _engine_context(serp_status=200, serp_text=None, pool=None):
    http = FakeHttp(default_status=serp_status,
                    default_text=serp_text or load_serp("bing_cn.html"))
    cfg = Config(cache_enabled=False, rate_limit_enabled=False, render_enabled=True)
    fetcher = SmartFetcher(cfg, http)
    pool = pool or FakePool()
    fetcher._pool = pool
    ctx = ProviderContext(http=http, config=cfg, fetcher=fetcher)
    return ctx, http, pool, fetcher


def test_provider_works_through_real_fetcher():
    """检索源必须能经由 SmartFetcher 正常解析(不是只有 http.get 能跑通)。"""
    ctx, http, pool, _ = _engine_context()
    rs = REGISTRY["bing_html"]().search("铭凡 UM880 Pro NPU",
                                        QueryOptions(top_k=5, retries=0), ctx)
    assert len(rs) >= 5, f"经 fetcher 解析结果过少: {len(rs)}"


def test_query_params_reach_http_client():
    """查询串必须真的传到底层 —— 少了 params 会让所有源 TypeError。"""
    ctx, http, _, _ = _engine_context()
    REGISTRY["bing_html"]().search("AMD Zen5", QueryOptions(retries=0), ctx)
    assert http.calls, "底层 HTTP 未被调用"
    assert any("q" in (kw.get("params") or {}) for kw in http.kwargs), \
        f"查询串未传到底层: {http.kwargs}"


def test_serp_is_not_escalated_to_render():
    """搜索结果页不得被启发式升级到渲染: 渲染后 DOM 结构改变会毁掉解析。"""
    ctx, _, pool, _ = _engine_context()
    rs = REGISTRY["bing_html"]().search("铭凡 UM880 Pro NPU",
                                        QueryOptions(top_k=5, retries=0), ctx)
    assert pool.calls == [], f"结果页被误升级到渲染: {pool.calls}"
    assert len(rs) >= 5


def test_blocked_serp_still_gets_render_rescue():
    """HTTP 真被拦截(403)时, 渲染救援仍应发生 —— 救援通道不能被一起关掉。"""
    from intellisearch.errors import BlockedError
    http = FakeHttp(default_text=BlockedError("疑似被拦截 status=403"))
    cfg = Config(cache_enabled=False, rate_limit_enabled=False, render_enabled=True)
    fetcher = SmartFetcher(cfg, http)
    pool = FakePool(html=load_serp("bing_cn.html"))
    fetcher._pool = pool
    ctx = ProviderContext(http=http, config=cfg, fetcher=fetcher)
    rs = REGISTRY["bing_html"]().search("AMD Zen5", QueryOptions(retries=0), ctx)
    assert pool.calls, "403 被拦截时未触发渲染救援"
    assert len(rs) >= 5, "救援渲染拿到的页面应能解析出结果"


def test_challenge_page_does_not_waste_render():
    """200 但返回几 KB 的验证页: 识别为被拦, 但**不**启动渲染。

    原因(实测): 浏览器重渲染出的 SERP 是另一棵树 —— 360 渲染后拿到 326 KB
    的真实页面, 里面 `res-list` 却为 0; Bing 渲染后 `b_algo` 从 10 变 0。
    选择器全部失效, 结果仍是 0 条, 却要多付 5~11 秒。
    """
    ctx, _, pool, _ = _engine_context(
        serp_status=200, serp_text="<html><body>访问异常页面, 请完成安全验证</body></html>")
    REGISTRY["bing_html"]().search("AMD Zen5", QueryOptions(retries=0), ctx)
    assert pool.calls == [], "小体积验证页不应再启动注定无效的渲染"


def test_hard_status_response_triggers_rescue():
    """403 响应(而不是抛异常)同样必须触发救援 —— 覆盖 `_needs_rescue` 状态分支。"""
    http = FakeHttp(default_status=403, default_text="<html><body>403</body></html>")
    cfg = Config(cache_enabled=False, rate_limit_enabled=False, render_enabled=True)
    fetcher = SmartFetcher(cfg, http)
    pool = FakePool(html=load_serp("bing_cn.html"))
    fetcher._pool = pool
    ctx = ProviderContext(http=http, config=cfg, fetcher=fetcher)
    rs = REGISTRY["bing_html"]().search("AMD Zen5", QueryOptions(retries=0), ctx)
    assert pool.calls, "403 响应未触发渲染救援"
    assert len(rs) >= 5, "救援渲染拿到的页面应能解析出结果"


def test_forced_render_is_never_skipped_by_dead_host_memo():
    """用户显式 mode='render' 时, 内部"省时间"记忆绝不能把它挡下来。"""
    http = FakeHttp(default_text=load_serp("bing_cn.html"))
    cfg = Config(cache_enabled=False, rate_limit_enabled=False, render_enabled=True)
    fetcher = SmartFetcher(cfg, http)
    pool = FakePool()
    fetcher._pool = pool
    pool.html = ""                      # 渲染无产出 -> 该主机被记住
    first = fetcher.fetch("https://cn.bing.com/search", mode="auto")
    assert first.fetch_mode == "http", first.attempts
    pool.html = load_serp("bing_cn.html")
    forced = fetcher.fetch("https://cn.bing.com/search", mode="render")
    assert forced.fetch_mode == "render", forced.attempts
    assert forced.rendered


def test_hard_block_still_rescues_but_does_not_repeat():
    """403 这类硬拦截仍要救援一次; 但该主机渲染确认无产出后不再重复付代价。"""
    from intellisearch.errors import BlockedError
    http = FakeHttp(default_text=BlockedError("疑似被拦截 status=403"))
    cfg = Config(cache_enabled=False, rate_limit_enabled=False, render_enabled=True)
    fetcher = SmartFetcher(cfg, http)
    pool = FakePool(html="<html><body>403</body></html>", status=403)
    fetcher._pool = pool
    ctx = ProviderContext(http=http, config=cfg, fetcher=fetcher)
    opts = QueryOptions(retries=0)
    for _ in range(2):
        # 渲染后仍是 403 → 源确实被封, provider 抛异常是正确行为
        with pytest.raises(Exception):
            REGISTRY["bing_html"]().search("AMD Zen5", opts, ctx)
    assert len(pool.calls) == 1, f"已确认无产出的主机不应重复渲染: {pool.calls}"


def test_large_page_with_verification_words_is_not_rescued():
    """正常大结果页里出现"验证码"字样(比如搜索该词)时, 绝不能升级渲染。"""
    big = load_serp("bing_cn.html") + "<p>验证码 安全验证</p>" * 200
    ctx, _, pool, _ = _engine_context(serp_status=200, serp_text=big)
    rs = REGISTRY["bing_html"]().search("验证码", QueryOptions(retries=0), ctx)
    assert pool.calls == [], "大结果页被误判为验证页"
    assert len(rs) >= 5


@pytest.mark.parametrize("name,fixture", [("bing_html", "bing_cn.html"),
                                          ("so360", "so360.html"),
                                          ("sogou", "sogou.html")])
def test_all_local_providers_survive_fetcher_path(name, fixture):
    """三个本地源都必须在 fetcher 路径下解析出结果(防止再出现全源归零)。"""
    ctx, _, pool, _ = _engine_context(serp_text=load_serp(fixture))
    rs = REGISTRY[name]().search("测试", QueryOptions(top_k=5, retries=0), ctx)
    assert rs, f"{name} 经 fetcher 解析为空"
    assert pool.calls == [], f"{name} 被误升级到渲染"
