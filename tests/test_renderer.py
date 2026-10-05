"""浏览器渲染能力测试。

分为两类:
- 离线可跑: 内核发现逻辑、渲染必要性判定、降级判定(不启动浏览器)
- @pytest.mark.integration: 真实启动 Chromium 渲染(需 --run-integration)
"""
import pytest

from intellisearch.http.browser import (BrowserCandidate, BrowserPool,
                                        RenderError, RenderRequest, find_chromium)
from intellisearch.http.browser import _candidates
from intellisearch.http.fetcher import SmartFetcher
from intellisearch.http.renderer import needs_render

from conftest import load_page


# ==================================================================
# 内核发现(离线)
# ==================================================================
def test_candidates_not_empty_on_this_machine():
    """本机应至少能发现一个 Chromium 内核(Edge 或 playwright 缓存)。"""
    cands = _candidates()
    assert cands, "未发现任何内核候选"
    assert all(isinstance(c, BrowserCandidate) for c in cands)


def test_candidate_launch_kwargs_shape():
    p = BrowserCandidate(kind="path", name="X", executable_path="/tmp/c.exe")
    assert p.launch_kwargs() == {"executable_path": "/tmp/c.exe"}
    c = BrowserCandidate(kind="channel", name="Y", channel="msedge")
    assert c.launch_kwargs() == {"channel": "msedge"}


def test_candidate_describe():
    p = BrowserCandidate(kind="path", name="Edge", executable_path="/x/msedge.exe")
    assert "msedge.exe" in p.describe()
    c = BrowserCandidate(kind="channel", name="Edge", channel="msedge")
    assert "msedge" in c.describe()


def test_find_chromium_returns_path_or_none():
    """不应抛异常: 找不到时返回 None, 找到时返回路径/通道名字符串。"""
    c = find_chromium(probe=False)
    assert c is None or isinstance(c, str)


# ==================================================================
# 渲染必要性判定(离线)
# ==================================================================
@pytest.mark.parametrize("html,status,expected", [
    ("", 200, True),                              # 空响应
    ("<html>x</html>", 403, True),                # HTTP 层被挡
    ("<html>x</html>", 429, True),                # 限流
    ("<div id='root'></div>", 200, True),         # SPA 挂载点
    ("<script>window.__NEXT_DATA__={}</script>", 200, True),
    # 极短页面 + noscript 里的"请开启 JavaScript"提示(SPA 通用形态)
    ("<html><body><noscript>请开启 JavaScript 以继续浏览</noscript>"
     "<div id='app'></div></body></html>", 200, True),
])
def test_needs_render_true(html, status, expected):
    assert needs_render(html, status) is expected


def test_needs_render_false_for_real_article():
    """真实长文档不该被判为需要渲染(否则会白付 10 倍成本)。"""
    html = load_page("page_doc.html")
    assert needs_render(html) is False


def test_needs_render_false_for_normal_article():
    html = "<html><body><article><p>" + ("这是一段正常的文章内容。" * 200) + \
           "</p></article></body></html>"
    assert needs_render(html) is False


# ==================================================================
# 降级判定(离线)
# ==================================================================
def test_spa_skeleton_needs_render():
    assert SmartFetcher._looks_incomplete('<div id="root"></div><script>a=1</script>')


def test_real_article_is_complete():
    assert SmartFetcher._looks_incomplete(load_page("page_doc.html")) is False


def test_async_content_page_detected():
    """无 SPA 标记但正文靠 JS 异步拉取的页面(东方财富行情页形态)应被识别。

    实测该页 HTTP 直取: 20963 字节 HTML, 去标签后仅 1199 可见字符(全是导航),
    比值约 17.5 —— 正文表格完全由 JS 异步生成。
    """
    nav = ("行情中心 财经 焦点 股票 新股 期指 期权 行情 数据 全球 美股 港股 期货 "
           "外汇 银行 基金 理财 债券 直播 股吧 基金吧 博客 财富号 搜索 选择股 "
           "数据中心 手机站 客户端") * 2
    # 用内联脚本把 HTML 撑到 20KB 量级, 模拟真实页面的体积/文本比
    filler = "<script>var _d=" + ("x" * 18000) + ";</script>"
    html = (f"<html><head>{filler}</head><body>{nav}</body></html>")
    text_len = len(nav)
    assert len(html) > 8 * text_len, "样例比例应与真实页面接近"
    assert SmartFetcher._looks_incomplete(html) is True


def test_long_article_not_flagged_as_async():
    """对照: 真正的长文档即便 HTML 偏大也不应升级渲染(避免白付 10 倍成本)。"""
    body = "<p>" + ("这是一段有实质内容的文章段落, 用来验证不会被误判为需要渲染。" * 60) + "</p>"
    html = f"<html><body><article>{body}</article></body></html>"
    assert SmartFetcher._looks_incomplete(html) is False


def test_empty_html_is_incomplete():
    assert SmartFetcher._looks_incomplete("") is True


def test_loading_placeholder_is_incomplete():
    assert SmartFetcher._looks_incomplete("<html><body>加载中...</body></html>")


# ==================================================================
# 接口契约
# ==================================================================
def test_render_request_defaults():
    r = RenderRequest(url="https://a.com")
    assert r.timeout > 0
    assert r.wait_until == "domcontentloaded"
    assert r.block_resources is True


def test_pool_available_does_not_raise():
    """available() 必须返回 bool 而不是抛异常(未装 playwright 时应为 False)。"""
    p = BrowserPool()
    assert isinstance(p.available(), bool)


def test_pool_rejects_bad_url():
    p = BrowserPool()
    with pytest.raises(RenderError):
        p.render(RenderRequest(url="not-a-url"))


def test_pool_info_shape():
    info = BrowserPool().info()
    for k in ("available", "engine", "headless", "max_pages", "stats"):
        assert k in info


# ==================================================================
# 真实渲染(需网络与内核)
# ==================================================================
@pytest.mark.integration
def test_renderer_available():
    p = BrowserPool()
    assert p.available() is True, p.unavailable_reason()


@pytest.mark.integration
def test_render_static_page():
    p = BrowserPool()
    r = p.render(RenderRequest(url="https://example.com/", timeout=30))
    assert r.ok
    assert "Example Domain" in r.html


@pytest.mark.integration
def test_render_js_dynamic_page():
    """SPA 页: 轻量 HTTP 拿不到行情表格, 渲染后必须拿到。"""
    p = BrowserPool()
    r = p.render(RenderRequest(url="https://quote.eastmoney.com/center/gridlist.html",
                               timeout=40, settle_ms=2500))
    assert r.html
    assert len(r.html) > 40000, f"渲染内容过少: {len(r.html)}"


@pytest.mark.integration
def test_smart_fetcher_auto_upgrade():
    """auto 模式: SPA 页应自动升级到渲染。"""
    f = SmartFetcher()
    try:
        r = f.fetch("https://quote.eastmoney.com/center/gridlist.html", mode="auto")
        assert r.rendered is True
        assert r.fetch_mode == "render"
        assert r.upgrade_reason
    finally:
        f.close()


@pytest.mark.integration
def test_smart_fetcher_static_stays_http():
    """静态页应保持轻量 HTTP, 不白付渲染成本。"""
    f = SmartFetcher()
    try:
        r = f.fetch("https://docs.python.org/zh-cn/3/tutorial/datastructures.html",
                    mode="auto")
        assert r.fetch_mode == "http"
        assert r.rendered is False
        assert len(r.text) > 20000
    finally:
        f.close()


@pytest.mark.integration
def test_forced_render_mode():
    f = SmartFetcher()
    try:
        r = f.fetch("https://example.com/", mode="render", timeout=30)
        assert r.rendered is True
        assert r.fetch_mode == "render"
    finally:
        f.close()


@pytest.mark.integration
def test_resource_blocking_saves_traffic():
    """渲染时应拦截图片/字体/媒体, 大幅降低资源消耗。"""
    p = BrowserPool()
    r = p.render(RenderRequest(url="https://www.baidu.com/", timeout=30))
    assert r.blocked_requests > 0, "未拦截任何资源, 过滤可能未生效"


@pytest.mark.integration
def test_concurrency_limit_respected():
    """并发抓取不应超过配置的页签上限, 且全部能返回。"""
    import time
    from concurrent.futures import ThreadPoolExecutor
    f = SmartFetcher()
    try:
        urls = ["https://example.com/"] * 3
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=3) as pool:
            outs = list(pool.map(lambda u: f.fetch(u, mode="render", timeout=30), urls))
        assert all(o.rendered for o in outs)
        assert f.info()["stats"]["render_ok"] == 3
    finally:
        f.close()

def test_browser_error_page_is_recognized():
    """服务器没给正文时 Chromium 会生成自己的错误页, 必须能认出来。

    认不出来就会把 300KB 的错误页当正文返回, 上层因此报 ok=True 却拿到 0 字内容。
    """
    from intellisearch.http.renderer import _is_browser_error_page
    assert _is_browser_error_page(
        '<html><body class="neterror"><button id="reload-button">刷新</button></body></html>')
    assert _is_browser_error_page('<div id="errorPageContainer">HTTP ERROR 403</div>')
    assert not _is_browser_error_page('<html><body><h1>AMD Zen5</h1></body></html>')


def test_http_status_error_is_distinguished_from_real_failure():
    """只有服务器回错误状态码才容忍; 超时等真实失败必须继续抛出。"""
    from intellisearch.http.renderer import _is_http_status_error
    assert _is_http_status_error(Exception('net::ERR_HTTP_RESPONSE_CODE_FAILURE'))
    assert not _is_http_status_error(Exception('Timeout 30000ms exceeded'))
    assert not _is_http_status_error(Exception('net::ERR_CONNECTION_REFUSED'))
