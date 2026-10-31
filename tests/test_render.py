"""渲染层与降级链测试。

离线部分不依赖浏览器; 需要真实渲染的用例标记 integration。

**不依赖外部站点**: 所有抓取/渲染用例都打本机临时 HTTP 服务
(conftest.py 的 ``local_site`` fixture)。
2026-10-31 的 CI 集成失败即由外部 example.com 对 GitHub runner 返回
Cloudflare 503 引起(渲染链路本身正常, 单测却红了两条):
  - test_render_static_page              -> assert 503 == 200
  - test_render_concurrent_requests      -> 2/4 (两个 example.com 都 503)
把被测页面换成本机服务后, 结果只由代码决定, 不再赌外部站点的可用性。
"""
import time

import pytest

from conftest import SPA_JS_MARKER, STATIC_PAGE_MARKER
from intellisearch.config import Config
from intellisearch.http.browser import (BrowserPool, RenderError, RenderRequest,
                                        find_chromium)
from intellisearch.http.fetcher import SmartFetcher


# ---------------------------------------------------------------- 内核探测
def test_find_chromium_returns_path_or_none():
    p = find_chromium()
    assert p is None or isinstance(p, str)


def test_pool_reports_engine_without_starting():
    pool = BrowserPool(Config(), max_pages=1)
    try:
        name = pool.engine_name()
        assert isinstance(name, str)
        # 归一化家族名; 不能是候选来源(which/system/playwright-bundled...)
        assert name in ("msedge", "chrome", "chromium", "brave", "none"), name
    finally:
        pool.close(timeout=5)


def test_pool_available_returns_bool():
    pool = BrowserPool(Config())
    try:
        assert isinstance(pool.available(), bool)
    finally:
        pool.close(timeout=5)


# ---------------------------------------------------------------- 降级链判定
def test_empty_html_needs_render():
    assert SmartFetcher._looks_incomplete("") is True


def test_spa_framework_markers_need_render():
    for marker in ('<div id="root"></div>', '<div id="app"></div>',
                   "<script>__NEXT_DATA__</script>",
                   "window.__INITIAL_STATE__", "ng-version=\"1.0\""):
        assert SmartFetcher._looks_incomplete(
            f"<html><body>{marker}</body></html>") is True, marker


def test_skeleton_page_detected_by_text_density():
    """HTML 体积大但可见文字极少 => SPA 骨架页。"""
    filler = "<div class='x'>y</div>" * 100
    html = f"<html><body><nav>Home</nav>{filler}</body></html>"
    assert SmartFetcher._looks_incomplete(html) is True


def test_normal_article_not_flagged():
    para = "<p>这是一段有实际内容的正文段落，用于测试不会被误判为骨架页。</p>"
    html = f"<html><body><article>{para * 12}</article></body></html>"
    assert SmartFetcher._looks_incomplete(html) is False


def test_static_page_not_flagged():
    html = ("<html><body><h1>Example Domain</h1>"
            "<p>This domain is for use in illustrative examples.</p>"
            "</body></html>")
    assert SmartFetcher._looks_incomplete(html) is False


# ---------------------------------------------------------------- 抓取模式
def test_http_mode_never_touches_browser(local_site):
    """mode=http 时不应启动浏览器, 且必须真的拿到本机页面的正文。"""
    f = SmartFetcher(Config(render_enabled=True))
    try:
        o = f.fetch(local_site + "/", mode="http", timeout=10, retries=0)
        assert o.fetch_mode == "http"
        assert o.rendered is False
        assert o.ok is True
        assert STATIC_PAGE_MARKER in o.text
    finally:
        f.close()


def test_fetch_outcome_fields_present(local_site):
    f = SmartFetcher(Config(render_enabled=False))
    try:
        o = f.fetch(local_site + "/", mode="http", timeout=10, retries=0)
        for attr in ("text", "url", "status", "fetch_mode", "degraded",
                     "attempts", "elapsed_ms", "upgrade_reason"):
            assert hasattr(o, attr), attr
        assert o.attempts, "必须记录逐级尝试轨迹"
    finally:
        f.close()


def test_fetcher_info_shape():
    f = SmartFetcher(Config(render_enabled=False))
    try:
        info = f.info()
        for k in ("render_enabled", "render_available", "engine", "stats"):
            assert k in info
    finally:
        f.close()


def test_render_disabled_config_makes_pool_none():
    f = SmartFetcher(Config(render_enabled=False))
    try:
        assert f.pool is None
        assert f.render_available() is False
    finally:
        f.close()


# ---------------------------------------------------------------- 真实渲染
# 浏览器启动/关闭开销大(单次约 8s), 因此真实渲染用例共用一个模块级池,
# 既贴近实际使用方式(全局复用), 也避免每条用例重复启停。
_SHARED_POOL = None


def _pool():
    global _SHARED_POOL
    if _SHARED_POOL is None:
        p = BrowserPool(Config(), max_pages=2)
        if not p.available():
            pytest.skip("无 playwright 或未安装 Chromium 内核")
        _SHARED_POOL = p
    return _SHARED_POOL


@pytest.fixture(scope="module", autouse=True)
def _close_shared_pool():
    """模块结束时关闭共享浏览器池。

    之前把 pytest_unconfigure 定义在测试模块里 —— pytest 只从 conftest.py /
    插件收集该钩子, 放在这里永远不执行, 浏览器进程会随会话泄漏。
    """
    yield
    global _SHARED_POOL
    if _SHARED_POOL is not None:
        _SHARED_POOL.close(timeout=10)
        _SHARED_POOL = None


@pytest.mark.integration
def test_render_spa_page(local_site):
    """SPA 页面必须靠渲染才能拿到 JS 注入的内容。"""
    pool = _pool()
    r = pool.render(RenderRequest(
        url=local_site + "/spa", timeout=25, wait_selector="#app"))
    assert r.status == 200
    assert SPA_JS_MARKER in r.html, "JS 注入后的内容没有出现在渲染结果里"
    # engine 必须是归一化家族名(回归: CI 上曾输出候选来源 "which")
    assert r.engine in ("chromium", "chrome", "msedge", "brave"), r.engine


@pytest.mark.integration
def test_render_static_page(local_site):
    pool = _pool()
    r = pool.render(RenderRequest(url=local_site + "/", timeout=25))
    assert r.status == 200 and r.html
    assert STATIC_PAGE_MARKER in r.html


@pytest.mark.integration
def test_render_timeout_is_enforced(local_site):
    """超长加载时间的页面必须在设定时限内失败, 而不是无限等待。"""
    pool = _pool()
    before = pool.stats["timeouts"]
    t0 = time.time()
    with pytest.raises(RenderError):
        pool.render(RenderRequest(url=local_site + "/slow", timeout=3))
    elapsed = time.time() - t0
    assert elapsed < 25, f"超时未在合理时间内生效: {elapsed:.1f}s"
    assert pool.stats["timeouts"] > before


@pytest.mark.integration
def test_render_concurrent_requests(local_site):
    """并发渲染: 验证信号量排队与事件循环并发都正常。

    4 个请求全部打到本机服务 —— 之前混用 example.com 时, 上游 503 会让
    并发成功率断言(>=3)变成外部可用性探针。
    """
    from concurrent.futures import ThreadPoolExecutor

    pool = _pool()
    urls = [local_site + "/", local_site + "/spa"] * 2

    def work(u):
        return pool.render(RenderRequest(url=u, timeout=30))

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(work, urls))

    assert len(results) == 4
    for r in results:
        assert r.status == 200 and r.html, f"渲染失败: {r.url} status={r.status}"
    # 两条 SPA 请求必须都拿到了 JS 注入后的内容
    assert sum(SPA_JS_MARKER in r.html for r in results) == 2


@pytest.mark.integration
def test_fetcher_auto_upgrade_to_render(local_site):
    """auto 模式遇到 SPA 骨架页应自动升级到渲染。"""
    pool = _pool()          # 确保内核可用
    f = SmartFetcher(Config(render_enabled=True), pool=pool)
    try:
        o = f.fetch(local_site + "/spa", mode="auto", timeout=30)
        assert o.fetch_mode == "render"
        assert o.rendered is True
        assert o.upgrade_reason
        assert SPA_JS_MARKER in o.text
    finally:
        f.http.close()


@pytest.mark.integration
def test_nonexistent_host_degrades_cleanly():
    """目标完全不可达时也不能抛异常, 应干净降级。"""
    f = SmartFetcher(Config(render_enabled=True))
    try:
        o = f.fetch("https://no-such-host-xyz987654.com/", mode="auto", timeout=12)
        assert o.degraded is True
        assert o.fetch_mode == "none"
        assert o.reason
        assert len(o.attempts) >= 1
    finally:
        f.close()
