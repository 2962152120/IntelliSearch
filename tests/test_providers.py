"""检索源解析测试(全部离线, 使用保存的样本页)。"""
import pytest

from intellisearch.models import QueryOptions, parse_time
from intellisearch.providers import REGISTRY
from intellisearch.providers.base import ProviderContext

from conftest import FakeHttp


def _run(name, fixture, query="铭凡 UM880 Pro NPU", **opts_kw):
    from conftest import load_serp
    http = FakeHttp(default_text=load_serp(fixture))
    ctx = ProviderContext(http=http)
    opts = QueryOptions(top_k=5, timeout=5, retries=0, **opts_kw)
    return REGISTRY[name]().search(query, opts, ctx)


def test_bing_rss_parses_items():
    from conftest import load_serp
    from intellisearch.providers.bing_rss import BingRSSProvider
    xml = load_serp("bing_rss.xml")
    items = BingRSSProvider()._parse(xml)
    assert len(items) >= 5
    for it in items:
        assert it.title and it.url.startswith("http")
    assert any(i.publish_time for i in items), "RSS 应能解析出 pubDate"


def test_bing_rss_pubdate_chinese_format():
    assert parse_time("周五, 02 10月 2026 20:31:00 GMT") is not None


def test_bing_html_extracts_results():
    rs = _run("bing_html", "bing_cn.html")
    assert len(rs) >= 5, f"解析结果过少: {len(rs)}"
    for r in rs:
        assert r.title and r.url.startswith("http")
        assert r.source == "bing_html"


def test_bing_html_title_order_correct():
    """高亮词应在标题中的正确位置, 而不是被丢到末尾。"""
    rs = _run("bing_html", "bing_cn.html")
    assert any(r.title.startswith("铭") for r in rs), [r.title for r in rs[:3]]


def test_sogou_extracts_results_with_dates():
    rs = _run("sogou", "sogou.html")
    assert len(rs) >= 3
    assert any(r.publish_time for r in rs), "搜狗结果应带发布时间"
    assert all(r.url.startswith("http") for r in rs)


def test_so360_extracts_results():
    rs = _run("so360", "so360.html")
    assert len(rs) >= 1
    for r in rs:
        assert r.title and r.url


def test_provider_raises_on_bad_status():
    from intellisearch.errors import UpstreamError
    http = FakeHttp(default_status=500, default_text="err")
    ctx = ProviderContext(http=http)
    with pytest.raises(UpstreamError):
        REGISTRY["bing_html"]().search("x", QueryOptions(retries=0), ctx)


def test_site_filter_appended_to_query():
    from intellisearch.providers.bing_rss import BingRSSProvider
    seen = {}

    class Spy(FakeHttp):
        def get(self, url, **kw):
            seen.update(kw.get("params") or {})
            return super().get(url, **kw)

    from conftest import load_serp
    http = Spy(default_text=load_serp("bing_rss.xml"))
    ctx = ProviderContext(http=http)
    BingRSSProvider().search("python", QueryOptions(site="github.com"), ctx)
    assert "site:github.com" in seen["q"]


def test_api_provider_disabled_without_key():
    from intellisearch.config import Config
    p = REGISTRY["tavily"](Config(tavily_key=""))
    assert p.is_available() is False
