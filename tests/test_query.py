"""Query 解析与改写测试。"""
from intellisearch.models import Freshness, QueryOptions
from intellisearch.retrieval.query import (
    core_terms, extract_site, extract_time, parse_boolean, rewrite, tokenize)


def test_tokenize_keeps_model_numbers():
    toks = tokenize("铭凡 UM880 Pro R7-8845HS")
    assert "UM880" in toks
    assert "铭凡" in toks


def test_tokenize_no_cross_word_bigrams():
    """不再产生 '力多' 这类跨词边界的垃圾 token。"""
    toks = tokenize("算力多少")
    assert "力多" not in toks
    assert "算力" in toks


def test_core_terms_drops_stopwords():
    terms = core_terms("请问 Python 的列表推导式怎么用")
    assert "Python" in terms
    assert not any(t in ("的", "请", "怎么") for t in terms)


def test_rewrite_matches_spec_example():
    """需求文档示例: 铭凡UM880 Pro的NPU算力多少 -> 含 NPU / TOPS / 参数。"""
    p = rewrite("铭凡UM880 Pro的NPU算力多少", QueryOptions())
    for kw in ("UM880", "NPU", "算力"):
        assert kw in p.effective, p.effective
    assert "TOPS" in p.effective or "参数" in p.effective
    assert p.rewritten is True


def test_rewrite_price_intent():
    p = rewrite("iPhone 16 多少钱", QueryOptions())
    assert "价格" in p.effective or "报价" in p.effective


def test_boolean_not_and_exclusion_removed():
    must, should, nots, phrases, cleaned = parse_boolean("rust async -tokio")
    assert "tokio" in nots
    assert "tokio" not in cleaned
    p = rewrite("rust async runtime -tokio", QueryOptions())
    assert "tokio" not in p.effective


def test_boolean_phrase_extracted():
    must, should, nots, phrases, cleaned = parse_boolean('"exact phrase" other')
    assert phrases == ["exact phrase"]


def test_boolean_or_produces_should():
    must, should, nots, phrases, cleaned = parse_boolean("cats OR dogs")
    assert len(should) == 2


def test_site_filter():
    site, rest = extract_site("python asyncio site:docs.python.org")
    assert site == "docs.python.org"
    assert "site:" not in rest


def test_time_range_parsing():
    assert extract_time("最近一周 AI 芯片")[0] == Freshness.WEEK
    assert extract_time("近1天新闻")[0] == Freshness.DAY
    assert extract_time("近一个月基金")[0] == Freshness.MONTH
    assert extract_time("Python 教程")[0] == Freshness.ANY


def test_time_phrase_stripped_from_query():
    """时间语义交给 freshness, 不再污染关键词。"""
    p = rewrite("最近一周 AI 芯片 发布", QueryOptions())
    assert p.effective.strip() != ""
    assert "周" not in p.terms


def test_custom_time_range():
    f, t_from, t_to, rest = extract_time("2024-01-01 到 2024-06-30 半导体")
    assert t_from is not None and t_to is not None
    assert t_from.year == 2024 and t_to.month == 6


def test_plain_keywords_are_not_broken():
    p = rewrite("Rust async runtime", QueryOptions())
    assert p.effective == "Rust async runtime"


def test_english_question_stripped():
    p = rewrite("What is the NPU performance of UM880 Pro", QueryOptions())
    assert "UM880" in p.effective
    assert "what" not in p.effective.lower().split()


def test_external_rewriter_used():
    p = rewrite("任意问题", QueryOptions(), rewriter=lambda q: ["改写结果A"])
    assert "改写结果A" in p.expanded
