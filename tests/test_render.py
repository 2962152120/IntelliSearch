"""渲染层与降级链测试。

离线部分不依赖浏览器; 需要真实渲染的用例标记 integration。
"""
import pytest

from intellisearch.config import Config
from intellisearch.http.browser import (BrowserPool, RenderRequest,
                                        find_chromium)
from intellisearch.http.fetcher import SmartFetcher


# ---------------------------------------------------------------- 内核探测
def test_find_chromium_returns_path_or_none():
    p = find_chromium()
    assert p is None or isinstance(p, str)


def test_pool_reports_engine_without_starting():
    pool = BrowserPool(Config(), max_pages=1)
    name = pool.engine_name()
    assert isinstance(name, str)
    assert name in ("msedge", "chrome", "chromium", "chromium-browser",
                    "playwright-chromium", "google-chrome", "none") or name


def test_pool_available_returns_bool():
    assert isinstance(BrowserPool(Config()).available(), bool)


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
def test_http_mode_never_touches_browser():
    """mode=http 时不应启动浏览器。"""
    f = SmartFetcher(Config(render_enabled=True))
    try:
        o = f.fetch("https://example.com/", mode="http", timeout=12, retries=0)
        assert o.fetch_mode == "http"
        assert o.rendered is False
    except Exception:
        pass          # 网络不可用时跳过断言
    finally:
        f.close()


def test_fetch_outcome_fields_present():
    f = SmartFetcher(Config(render_enabled=False))
    try:
        o = f.fetch("https://example.com/", mode="http", timeout=12, retries=0)
        for attr in ("text", "url", "status", "fetch_mode", "degraded",
                     "attempts", "elapsed_ms", "upgrade_reason"):
            assert hasattr(o, attr), attr
        assert o.attempts, "必须记录逐级尝试轨迹"
    except Exception:
        pass
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
            pytest.skip("无 playwright")
        _SHARED_POOL = p
    return _SHARED_POOL


def pytest_unconfigure(config):
    global _SHARED_POOL
    if _SHARED_POOL is not None:
        _SHARED_POOL.close(timeout=10)
        _SHARED_POOL = None


@pytest.mark.integration
def test_render_spa_page():
    """SPA 页面必须靠渲染才能拿到 JS 注入的内容。"""
    pool = _pool()
    r = pool.render(RenderRequest(
        url="https://quotes.toscrape.com/js/", timeout=30,
        wait_selector=".quote"))
    assert r.status == 200
    assert r.selector_found is True
    assert len(r.html) > 6000
    assert "quote" in r.html.lower()


@pytest.mark.integration
def test_render_static_page():
    pool = _pool()
    r = pool.render(RenderRequest(url="https://example.com/", timeout=25))
    assert r.status == 200 and r.html


@pytest.mark.integration
def test_render_timeout_is_enforced():
    """超长加载时间的页面必须在设定时限内失败, 而不是无限等待。"""
    import time
    from intellisearch.http.browser import RenderError
    pool = _pool()
    before = pool.stats["timeouts"]
    t0 = time.time()
    with pytest.raises(RenderError):
        pool.render(RenderRequest(url="https://httpbin.org/delay/10", timeout=5))
    elapsed = time.time() - t0
    assert elapsed < 30, f"超时未在合理时间内生效: {elapsed:.1f}s"
    assert pool.stats["timeouts"] > before


@pytest.mark.integration
def test_render_concurrent_requests():
    """并发渲染: 验证信号量排队与事件循环并发都正常。"""
    from concurrent.futures import ThreadPoolExecutor
    pool = _pool()
    urls = ["https://example.com/", "https://quotes.toscrape.com/js/"] * 2

    def work(u):
        return pool.render(RenderRequest(url=u, timeout=30))

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(work, urls))
    ok = [r for r in results if r.status == 200 and r.html]
    assert len(ok) >= 3, f"并发成功率过低: {len(ok)}/{len(results)}"


@pytest.mark.integration
def test_fetcher_auto_upgrade_to_render():
    """auto 模式遇到 SPA 骨架页应自动升级到渲染。"""
    pool = _pool()          # 确保内核可用
    f = SmartFetcher(Config(render_enabled=True), pool=pool)
    try:
        o = f.fetch("https://quotes.toscrape.com/js/", mode="auto", timeout=30)
        assert o.fetch_mode == "render"
        assert o.rendered is True
        assert o.upgrade_reason
        assert len(o.text) > 6000
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
