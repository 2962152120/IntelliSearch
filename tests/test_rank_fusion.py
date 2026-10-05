"""去重 / 排序 / 过滤 / 数量上限 测试。"""
from datetime import datetime, timedelta, timezone

from intellisearch.models import (Freshness, QueryOptions, SearchResult,
                                  SourceType, canonical_url)
from intellisearch.retrieval.fusion import dedupe, fuse
from intellisearch.retrieval.rank import (credibility_of, filter_results,
                                          limit_results, rank, score_freshness,
                                          score_relevance)


def mk(title, url, snippet="", days_ago=None, source="test", **kw):
    pt = None
    if days_ago is not None:
        pt = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return SearchResult(title=title, url=url, snippet=snippet,
                        publish_time=pt, source=source, **kw)


# ---------------------------------------------------------------- 去重
def test_canonical_url_strips_tracking_params():
    a = "https://www.Example.com/path/?utm_source=x&id=3#frag"
    b = "https://example.com/path?id=3"
    assert canonical_url(a) == canonical_url(b)


def test_dedupe_by_url():
    rs = [mk("T1", "https://a.com/x?utm_source=1"), mk("T1", "https://www.a.com/x"),
          mk("T2", "https://b.com/y")]
    out = dedupe(rs)
    assert len(out) == 2


def test_dedupe_by_similar_title_across_mirrors():
    """同源标题、不同 URL 的镜像内容应合并, 并记录来自多个检索源。"""
    rs = [mk("同一个标题内容很长很长", "https://a.com/1", source="bing_rss"),
          mk("同一个标题内容很长很长", "https://b.com/2", source="sogou")]
    out = dedupe(rs)
    assert len(out) == 1
    assert set(out[0].extra.get("sources", [])) == {"bing_rss", "sogou"}


def test_dedupe_merges_missing_fields():
    a = mk("T", "https://a.com/1", snippet="short")
    b = mk("T", "https://a.com/1", snippet="a much longer snippet", days_ago=3)
    out = dedupe([a, b])
    assert len(out) == 1
    assert len(out[0].snippet) > len("short")
    assert out[0].publish_time is not None


def test_dedupe_drops_unresolved_redirect_links():
    rs = [mk("T", "https://www.sogou.com/link?url=abc"), mk("ok", "https://a.com/1")]
    out = dedupe(rs)
    assert all("sogou.com/link" not in r.url for r in out)


# ---------------------------------------------------------------- 相关性
def test_relevance_title_hits_rank_higher():
    hi = mk("Python 列表推导式教程", "https://a.com/1")
    lo = mk("无关内容", "https://b.com/2")
    assert score_relevance(hi, ["Python", "列表推导式"]) > \
           score_relevance(lo, ["Python", "列表推导式"])


def test_freshness_decays_with_age():
    new = mk("T", "https://a.com", days_ago=1)
    old = mk("T", "https://a.com", days_ago=2000)
    assert score_freshness(new) > score_freshness(old)


def test_freshness_neutral_when_missing():
    assert score_freshness(mk("T", "https://a.com")) == 0.45


def test_credibility_uses_domain_table():
    good = mk("T", "https://docs.python.org/x")
    bad = mk("T", "https://random-blog.example/x")
    assert credibility_of(good) > credibility_of(bad)


def test_rank_orders_by_score():
    rs = [mk("无关", "https://c.com/3"),
          mk("Python 列表推导式 教程", "https://docs.python.org/1", days_ago=2)]
    ranked = rank(rs, ["Python", "列表推导式"], "Python 列表推导式")
    assert "Python" in ranked[0].title
    assert ranked[0].extra["_score"] >= ranked[-1].extra["_score"]


# ---------------------------------------------------------------- 过滤
def test_filter_by_site():
    rs = [mk("A", "https://github.com/x"), mk("B", "https://other.com/y")]
    kept, stats = filter_results(rs, QueryOptions(site="github.com"))
    assert len(kept) == 1 and "github.com" in kept[0].url
    assert stats["site"] == 1


def test_filter_exclude_sites():
    rs = [mk("A", "https://spam.com/x"), mk("B", "https://good.com/y")]
    kept, _ = filter_results(rs, QueryOptions(exclude_sites=["spam.com"]))
    assert all("spam.com" not in r.url for r in kept)


def test_filter_by_time_range():
    rs = [mk("old", "https://a.com/1", days_ago=400),
          mk("new", "https://a.com/2", days_ago=2)]
    since = datetime.now(timezone.utc) - timedelta(days=30)
    kept, stats = filter_results(rs, QueryOptions(), time_from=since)
    assert len(kept) == 1 and kept[0].title == "new"
    assert stats["time"] == 1


def test_filter_drops_search_engine_intermediate_pages():
    rs = [mk("AI摘要", "https://ai.so.com/search/so123?x=1"),
          mk("百科", "https://baike.baidu.com/item/1"),
          mk("真结果", "https://ithome.com/1")]
    kept, stats = filter_results(rs, QueryOptions())
    assert len(kept) == 2
    assert stats["serp"] == 1
    assert all("ai.so.com" not in r.url for r in kept)


def test_filter_drops_spam():
    rs = [mk("广告 立即下载app 推广", "https://a.com/1", snippet="广告 推广 点击购买"),
          mk("正常内容", "https://b.com/2", snippet="这是一段正常的正文内容介绍")]
    kept, stats = filter_results(rs, QueryOptions())
    assert stats["spam"] >= 1


def test_limit_results_caps_count():
    rs = [mk(f"T{i}", f"https://a.com/{i}") for i in range(20)]
    assert len(limit_results(rs, 5)) == 5


# ---------------------------------------------------------------- 融合
def test_fuse_end_to_end():
    from intellisearch.models import ParsedQuery
    rs = [mk("Python 列表推导式 官方教程", "https://docs.python.org/1", days_ago=3),
          mk("Python 列表推导式 官方教程", "https://docs.python.org/1"),
          mk("其它", "https://x.com/2", days_ago=900)]
    parsed = ParsedQuery(raw="Python 列表推导式", effective="Python 列表推导式",
                         terms=["Python", "列表推导式"])
    out, stats = fuse(rs, parsed, QueryOptions(top_k=5))
    assert len(out) == 2
    assert "Python" in out[0].title
    assert stats["deduped"] == 1


def test_multi_source_consensus_boost():
    from intellisearch.models import ParsedQuery
    a = mk("共识主题", "https://a.com/1", source="s1")
    b = mk("共识主题", "https://a.com/1", source="s2")
    parsed = ParsedQuery(raw="x", effective="共识主题", terms=["共识主题"])
    out, _ = fuse([a, b], parsed, QueryOptions(top_k=5))
    assert out[0].extra.get("source_count", 1) == 2
