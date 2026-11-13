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

from conftest import (FakeHttp, LOCAL_SITE_HITS, SPA_JS_MARKER,
                      STATIC_PAGE, STATIC_PAGE_MARKER)
from intellisearch.config import Config
from intellisearch.http.browser import (BrowserPool, RenderError, RenderRequest,
                                        RenderResult, find_chromium)
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


# ---------------------------------------------------------------- 渲染链降级/假成功
class _StubPool:
    """假浏览器池: 不启动浏览器, 原样返回预设的渲染结果。"""

    def __init__(self, result: RenderResult):
        self._result = result
        self.calls = []

    def available(self):
        return True

    def engine_name(self):
        return "chromium"

    def render(self, req):
        self.calls.append(req)
        return self._result

    def close(self, timeout=None):
        pass


def test_forced_render_mode_degrades_explicitly_when_disabled():
    """mode="render" 而渲染被禁用: 必须显式标注降级, 绝不静默退回 http。

    修复前 need_render 跟着 render_enabled 走, 这里会直接走 HTTP 提前返回:
    degraded=False、reason=""、fetch_mode="http" —— 调用方以为渲染成功了。
    """
    http = FakeHttp(default_text=STATIC_PAGE)
    f = SmartFetcher(Config(render_enabled=False), http=http)
    try:
        o = f.fetch("https://example.com/static", mode="render", timeout=5,
                    retries=0)
    finally:
        f.close()
    # 已拿到的正文与 ok 不能被破坏
    assert o.ok is True
    assert STATIC_PAGE_MARKER in o.text
    # tier 用实际路径
    assert o.fetch_mode == "http" and o.tier == "http"
    assert o.rendered is False
    # 但降级必须显式可见
    assert o.degraded is True
    assert "禁用" in o.reason and "降级" in o.reason, o.reason
    assert "无法满足 mode=render" in o.reason, o.reason
    assert "render:disabled" in o.attempts, o.attempts
    assert f.stats["degraded"] == 1
    assert f.stats["render_ok"] == 0


def test_auto_mode_with_render_disabled_stays_http():
    """对照: 渲染关闭时 mode="auto" 的静态页仍是预期路径, 不应标成降级。"""
    f = SmartFetcher(Config(render_enabled=False),
                     http=FakeHttp(default_text=STATIC_PAGE))
    try:
        o = f.fetch("https://example.com/static", mode="auto", timeout=5,
                    retries=0)
    finally:
        f.close()
    assert o.ok is True
    assert o.fetch_mode == "http" and o.tier == "http"
    assert o.degraded is False
    assert o.reason == ""


def test_forced_render_mode_reports_kernel_unavailable():
    """mode="render" 而内核不可用时: 走原"渲染内核不可用"提示并显式降级。"""
    class _UnavailablePool:
        def available(self):
            return False

        def engine_name(self):
            return "none"

        def close(self, timeout=None):
            pass

    f = SmartFetcher(Config(render_enabled=True),
                     http=FakeHttp(default_text=STATIC_PAGE),
                     pool=_UnavailablePool())
    try:
        o = f.fetch("https://example.com/static", mode="render", timeout=5,
                    retries=0)
    finally:
        f.close()
    assert o.ok is True
    assert o.degraded is True
    assert o.tier == "http"
    assert o.rendered is False
    assert "渲染内核不可用" in o.reason, o.reason
    assert "render:unavailable" in o.attempts, o.attempts
    assert f.stats["degraded"] == 1


def test_render_intercept_page_is_not_fake_success():
    """渲染返回 4xx/5xx 拦截页时不能算成功(假成功回归)。

    修复前只看 r.html 非空: 503 拦截页会被当成渲染成功 ——
    degraded=False、rendered=True、stats["render_ok"]++ 且 reason 被清空,
    与 _note_render(把 status>=400 记为 dead host)自相矛盾。
    """
    pool = _StubPool(RenderResult(status=503,
                                  html="<html>Cloudflare 拦截页</html>"))
    f = SmartFetcher(Config(render_enabled=True), http=FakeHttp(default_text=""),
                     pool=pool)
    try:
        o = f.fetch("https://blocked.example/x", mode="render", timeout=5,
                    retries=0)
    finally:
        f.close()
    assert pool.calls, "应尝试渲染"
    assert "render:503" in o.attempts, o.attempts
    assert "503" in o.reason, o.reason
    assert o.rendered is False
    assert o.degraded is True
    assert o.tier != "render" and o.fetch_mode != "render"
    assert o.ok is False
    assert "Cloudflare" not in o.text, "拦截页 html 被采信了"
    assert f.stats["render_ok"] == 0
    assert f.stats["degraded"] == 1


def test_render_intercept_page_does_not_overwrite_tier1_text():
    """拦截页 html 不得覆盖 Tier1 已拿到的正文, 应降级返回好内容。"""
    pool = _StubPool(RenderResult(status=403,
                                  html="<html>403 Forbidden</html>"))
    f = SmartFetcher(Config(render_enabled=True),
                     http=FakeHttp(default_text=STATIC_PAGE), pool=pool)
    try:
        o = f.fetch("https://example.com/static", mode="render", timeout=5,
                    retries=0)
    finally:
        f.close()
    assert STATIC_PAGE_MARKER in o.text
    assert "403 Forbidden" not in o.text
    assert o.status == 200
    assert o.ok is True               # 用 Tier1 的好内容降级返回
    assert o.rendered is False
    assert o.degraded is True
    assert o.tier == "http"
    assert "403" in o.reason, o.reason


def test_render_200_html_still_counts_as_success():
    """对照: status=200 的正常渲染成功路径不受影响。"""
    pool = _StubPool(RenderResult(status=200,
                                  html="<html><body>完整正文</body></html>"))
    f = SmartFetcher(Config(render_enabled=True), http=FakeHttp(default_text=""),
                     pool=pool)
    try:
        o = f.fetch("https://ok.example/x", mode="render", timeout=5, retries=0)
    finally:
        f.close()
    assert o.ok is True
    assert o.rendered is True
    assert o.fetch_mode == "render" and o.tier == "render"
    assert o.degraded is False
    assert o.reason == ""
    assert f.stats["render_ok"] == 1
    assert f.stats["degraded"] == 0


def test_render_status_zero_with_html_keeps_legacy_success():
    """status==0 但有 html(拿不到响应码)保持旧语义: 按成功, status 落 200。"""
    pool = _StubPool(RenderResult(status=0,
                                  html="<html><body>正文</body></html>"))
    f = SmartFetcher(Config(render_enabled=True), http=FakeHttp(default_text=""),
                     pool=pool)
    try:
        o = f.fetch("https://nocode.example/x", mode="render", timeout=5,
                    retries=0)
    finally:
        f.close()
    assert o.ok is True
    assert o.rendered is True
    assert o.status == 200
    assert o.degraded is False


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


@pytest.mark.integration
def test_render_block_resources_flag_is_wired(local_site):
    """RenderRequest.block_resources 必须真的控制资源拦截。

    回归: 该字段曾经没传给 renderer, 恒按 block_media=True 拦截,
    IS_RENDER_BLOCK_RES=0 完全无效。这里用本机站点的请求命中记录做硬证据:
    拦截时服务端**看不到** /img.png, 放行时必须看到。
    """
    pool = _pool()
    LOCAL_SITE_HITS.clear()
    blocked = pool.render(RenderRequest(
        url=local_site + "/pics", timeout=30, settle_ms=1500,
        block_resources=True))
    assert blocked.blocked_requests >= 1, "block_resources=True 未拦截任何资源"
    assert "/img.png" not in LOCAL_SITE_HITS, (
        f"图片请求不应到达服务端: {LOCAL_SITE_HITS}")

    LOCAL_SITE_HITS.clear()
    loaded = pool.render(RenderRequest(
        url=local_site + "/pics", timeout=30, settle_ms=1500,
        block_resources=False))
    assert loaded.blocked_requests == 0, "block_resources=False 仍在拦截资源"
    assert "/img.png" in LOCAL_SITE_HITS, (
        f"放行后图片请求应到达服务端: {LOCAL_SITE_HITS}")


@pytest.mark.integration
def test_render_lang_controls_locale(local_site):
    """RenderRequest.lang 必须影响 BrowserContext.locale(navigator.language)。

    回归: req.lang 曾经完全未接线, locale 恒为 zh-CN —— 抓英文站点时
    navigator.language 撒谎, 部分站点据此返回中文/降级页面。
    """
    pool = _pool()
    en = pool.render(RenderRequest(url=local_site + "/lang", timeout=30,
                                   lang="en", settle_ms=800))
    zh = pool.render(RenderRequest(url=local_site + "/lang", timeout=30,
                                   lang="zh", settle_ms=800))
    assert "en-US" in en.html, f"lang=en 未生效: {en.html[:300]}"
    assert "zh-CN" in zh.html, f"lang=zh 未生效: {zh.html[:300]}"

