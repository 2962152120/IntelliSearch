"""反爬软封防护: 整批结果与查询无关时必须丢弃。

真实场景(2026-10-05 实测): Bing 被限流后**不**返回 403/验证码页, 而是返回
`200 + 结构完整但内容全是热门无关内容`的结果页 —— 用"伊朗局势时间线"
"京东十月份活动"来回答 "AMD Zen5 架构"。解析器能正常解析, 计数也是 10,
但这些链接会被大模型当证据引用, **比没有结果危险得多**。
"""
from intellisearch.models import ParsedQuery, QueryOptions, SearchResult
from intellisearch.retrieval.fusion import (_drop_irrelevant,
                                            parallel_search)

from conftest import StaticProvider


def mk(title, url, snippet=""):
    return SearchResult(title=title, url=url, snippet=snippet)


def test_whole_batch_irrelevant_is_dropped():
    items = [mk("伊朗局势时间线", "https://news.example.com/1"),
             mk("京东十月份活动汇总", "https://shop.example.com/2")]
    kept, why = _drop_irrelevant(items, ["AMD", "Zen5", "架构"])
    assert kept == [], "整批无关的结果必须丢弃"
    assert "无关" in why


def test_single_term_hit_keeps_matching_result():
    """单实词查询: 标题/摘要命中即保留。

    多实词查询才要求"命中 >=2 个词" —— 单实词时再要求命中 2 个是不可能的,
    会把所有正常结果都杀光。
    """
    items = [mk("zen5 架构深度解析", "https://a.com/zen5-review")]
    kept, why = _drop_irrelevant(items, ["zen5"])
    assert len(kept) == 1 and why == ""


def test_multi_term_query_needs_more_than_one_hit():
    """多实词查询: 只命中 1 个词的泛化首页/词条页要被过滤掉。

    实测 "AMD Zen5 架构" 曾返回 amd.com 官网 / shop.amd.com ——
    只是域名里有 "amd", 与 Zen5 架构无关。
    """
    items = [mk("Advanced Micro Devices, Inc. (AMD)", "https://ir.amd.com/"),
             mk("AMD Online Store", "https://shop.amd.com/"),
             mk("AMD together we advance", "https://www.amd.com/en.html")]
    kept, why = _drop_irrelevant(items, ["AMD", "Zen5", "架构"])
    assert kept == [], "仅域名命中 AMD 的首页/商城页, 与 Zen5 架构无关"


def test_url_only_match_is_insufficient_for_multi_term_query():
    items = [mk("完全无关的标题", "https://a.com/zen5-review")]
    kept, why = _drop_irrelevant(items, ["zen5", "架构"])
    assert kept == [], "仅 URL 命中 1 个词, 证据不足"


def test_genuine_multi_term_results_survive():
    items = [mk("AMD Zen5 架构解析", "https://amd.com/zen5", "Zen5 微架构"),
             mk("Zen5 架构对比 Zen4", "https://x.com/1", "AMD Zen5")]
    kept, why = _drop_irrelevant(items, ["AMD", "Zen5", "架构"])
    assert len(kept) == 2 and why == ""


def test_no_terms_means_no_filtering():
    """查询词为空(全被停用词吃掉)时不能误杀。"""
    kept, why = _drop_irrelevant([mk("任意标题", "https://a.com/1")], [])
    assert len(kept) == 1 and why == ""


def test_match_counts_in_snippet():
    """命中出现在**摘要**里也算数(不只看标题)。"""
    items = [mk("完全无关的标题", "https://a.com/1", "本文讨论 zen5 微架构")]
    kept, why = _drop_irrelevant(items, ["zen5"])
    assert len(kept) == 1 and why == ""


def test_source_is_reported_as_failed_not_empty():
    """被判定不可用的源必须如实上报(ok=False + 原因), 而不是报 empty。"""

    class JunkProvider(StaticProvider):
        name = "junk"

        def search(self, query, options, ctx):
            return [mk("完全无关的热门内容", "https://x.com/1")]

    parsed = ParsedQuery(raw="AMD Zen5 架构", effective="AMD Zen5 架构",
                         terms=["AMD", "Zen5"])
    results, reports = parallel_search([JunkProvider()], parsed,
                                       QueryOptions(top_k=5), None, None)
    assert results == []
    assert reports[0].ok is False
    assert reports[0].status == "irrelevant"
    assert "无关" in reports[0].error


def test_relevant_source_passes_through():
    class GoodProvider(StaticProvider):
        name = "good"

        def search(self, query, options, ctx):
            return [mk("AMD Zen5 架构全面解析", "https://amd.example.com/1"),
                    mk("另一篇 AMD Zen5 文章", "https://x.example.com/2")]

    parsed = ParsedQuery(raw="AMD Zen5 架构", effective="AMD Zen5 架构",
                         terms=["AMD", "Zen5"])
    results, reports = parallel_search([GoodProvider()], parsed,
                                       QueryOptions(top_k=5), None, None)
    assert len(results) == 2
    assert reports[0].ok is True and reports[0].status == "ok"
