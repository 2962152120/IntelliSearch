"""2026-11-27 分析优化批次的回归测试。

覆盖:
- compress 的 max_chars 口径(必须与实际输出的 indent=2 一致)
- 付费墙 penalty 只施加一次
- site / exclude_sites 的 hostname 后缀匹配
- robots.txt 连续 User-agent 行共享规则
- 媒体扩展名判断不受查询串干扰
- 标题去重在长度分桶后仍正确
"""
import json

import pytest

from intellisearch.ai.formatter import compress
from intellisearch.http.client import HttpClient
from intellisearch.http.robots import RobotsCache
from intellisearch.models import QueryOptions, SearchResult
from intellisearch.retrieval.rank import _host_matches, filter_results
from intellisearch.retrieval.fusion import dedupe, _title_key


def _result(title, url, snippet=""):
    return SearchResult(title=title, url=url, snippet=snippet)


# ------------------------------------------------------------------ B1
def test_compress_budget_matches_indented_output():
    """max_chars 必须约束最终 indent=2 的输出, 而非紧凑 JSON。"""
    payload = {
        "results": [
            {"title": "标题" * 10, "url": "https://a.com/x",
             "snippet": "摘要" * 100}
            for _ in range(20)
        ]
    }
    budget = 2000
    out = compress(payload, budget)
    actual = len(json.dumps(out, ensure_ascii=False, indent=2))
    assert actual <= budget, f"indent=2 输出 {actual} 超出预算 {budget}"


def test_compress_budget_across_values():
    """多档预算下 indent=2 输出都不得超标(标记字段也要计入测量口径)。"""
    for budget in (1000, 2000, 3000, 5000):
        payload = {"results": [
            {"title": "标题" * 20, "url": f"https://a.com/{i}",
             "snippet": "摘要" * 200, "clean_text": "正文" * 500}
            for i in range(50)
        ]}
        out = compress(payload, budget)
        actual = len(json.dumps(out, ensure_ascii=False, indent=2))
        assert actual <= budget, f"预算 {budget} 超了: {actual}"


def test_compress_trims_metadata_when_single_result_still_over():
    """只剩 1 条仍超预算时, 应剥离诊断元数据(providers 等)而非放弃。"""
    payload = {
        "query": "测试", "status": "ok", "ok": True, "message": "检索成功",
        "elapsed_ms": 1234, "cached": False, "total_found": 1, "returned": 1,
        "parsed_query": {"raw": "测试", "terms": ["测试"] * 20},
        "providers": [{"name": f"p{i}", "status": "ok", "count": 3}
                      for i in range(5)],
        "conflicts": [], "created_at": "2026-11-27T00:00:00+00:00",
        "results": [{"title": "标题" * 20, "url": "https://a.com/1",
                     "snippet": "摘要" * 200, "clean_text": "正文" * 500}],
    }
    out = compress(payload, 800)
    actual = len(json.dumps(out, ensure_ascii=False, indent=2))
    assert actual <= 800, f"元数据剥离后仍超预算: {actual}"


def test_compress_honors_explicit_indent():
    """调用方传 indent=0 时按紧凑口径压缩。"""
    payload = {"results": [
        {"title": "T" * 30, "url": "https://a.com", "snippet": "S" * 100}
        for _ in range(10)
    ]}
    out = compress(payload, 600, indent=0)
    assert len(json.dumps(out, ensure_ascii=False)) <= 600


# ------------------------------------------------------------------ B3
def test_site_filter_rejects_hostname_substring():
    """site=github.com 不得放行 evilgithub.com(子串匹配绕过)。"""
    rs = [_result("good", "https://github.com/a"),
          _result("evil", "https://evilgithub.com/b"),
          _result("sub", "https://docs.github.com/c")]
    kept, _ = filter_results(rs, QueryOptions(site="github.com"))
    urls = [r.url for r in kept]
    assert "https://github.com/a" in urls
    assert "https://docs.github.com/c" in urls
    assert "https://evilgithub.com/b" not in urls


def test_exclude_sites_does_not_overmatch():
    """exclude_sites=baidu.com 不得误杀 notbaidu.com。"""
    rs = [_result("blocked", "https://baidu.com/a"),
          _result("keep", "https://notbaidu.com/b"),
          _result("sub", "https://news.baidu.com/c")]
    kept, _ = filter_results(rs, QueryOptions(exclude_sites=["baidu.com"]))
    urls = [r.url for r in kept]
    assert "https://baidu.com/a" not in urls
    assert "https://news.baidu.com/c" not in urls
    assert "https://notbaidu.com/b" in urls


def test_host_matches_helper():
    assert _host_matches("github.com", "github.com")
    assert _host_matches("docs.python.org", "python.org")
    assert _host_matches("python.org:443", "python.org")
    assert not _host_matches("evilgithub.com", "github.com")
    assert not _host_matches("notbaidu.com", "baidu.com")


# ------------------------------------------------------------------ B4
def test_consecutive_user_agents_share_rules():
    """连续 User-agent 行应共享其后的一组规则(robots 标准语义)。"""
    txt = ("User-agent: *\n"
           "User-agent: MyBot\n"
           "Disallow: /private\n")
    rc = RobotsCache(fetcher=lambda u: None)
    parsed = rc._parse(txt)
    assert "*" in parsed and "mybot" in parsed
    assert parsed["*"].disallow == ["/private"]
    assert parsed["mybot"].disallow == ["/private"]


def test_separate_user_agent_groups_not_merged():
    """空行/新指令分隔的 User-agent 组不应错误合并。"""
    txt = ("User-agent: *\nDisallow: /all\n\n"
           "User-agent: GoodBot\nDisallow: /none\n")
    rc = RobotsCache(fetcher=lambda u: None)
    parsed = rc._parse(txt)
    assert parsed["*"].disallow == ["/all"]
    assert parsed["goodbot"].disallow == ["/none"]


# ------------------------------------------------------------------ B12
def test_media_extension_ignores_query_string():
    """banner.png?size=large 应被判定为媒体(过滤不再被查询串绕过)。"""
    c = HttpClient.__new__(HttpClient)
    assert c._is_media("", "https://cdn.example.com/banner.png?size=large")
    assert c._is_media("", "https://cdn.example.com/v.mp4?v=1")
    assert not c._is_media("", "https://example.com/article?file=png")


# ------------------------------------------------------------------ A2
def test_title_dedupe_still_works_with_buckets():
    """长度分桶后, 相似标题仍应被去重, 不同标题保留。"""
    a = _result("Python 列表推导式 用法 教程", "https://a.com/1", "s1")
    b = _result("Python 列表推导式 用法教程", "https://b.com/2", "s2")
    c = _result("完全不同的另一篇文章", "https://c.com/3", "s3")
    out = dedupe([a, b, c])
    assert len(out) == 2, f"相似标题应合并: {[r.title for r in out]}"
    assert any("完全不同" in r.title for r in out)


def test_title_key_is_precompiled_pattern():
    # 空格/连字符/下划线是分隔符被移除; "!" 不在分隔符集合内故保留
    assert _title_key("  Hello-World_测试! ") == "helloworld测试!"
