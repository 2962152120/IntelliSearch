"""引擎行为测试: 降级 / 空结果 / 异常收敛 / 缓存 / 会话记忆 / 冲突检测。"""
import pytest

from intellisearch.ai.conflict import detect_conflicts, extract_facts
from intellisearch.ai.context import SessionStore, apply_context
from intellisearch.cache import Cache
from intellisearch.config import Config
from intellisearch.engine import SearchEngine
from intellisearch.errors import UpstreamError
from intellisearch.models import (QueryOptions, SearchResult, SourceType,
                                  Status)

from conftest import FailingProvider, FakeHttp, StaticProvider


def mk(title, url, snippet="", source="s1", **kw):
    return SearchResult(title=title, url=url, snippet=snippet, source=source, **kw)


def make_engine(providers, cache=None, cfg=None):
    cfg = cfg or Config(cache_enabled=False, rate_limit_enabled=False,
                        audit_log=False)
    e = SearchEngine(config=cfg, http=FakeHttp())
    e.providers = providers
    e.cache = cache or Cache(path=":memory:", enabled=False)
    return e


# ---------------------------------------------------------------- 状态
def test_status_ok_when_results_found():
    e = make_engine([StaticProvider("s1", [mk("Python 教程", "https://a.com/1")])])
    r = e.search("Python 教程", QueryOptions())
    assert r.status == Status.OK and r.ok and len(r.results) == 1


def test_status_partial_when_one_source_fails():
    e = make_engine([StaticProvider("s1", [mk("Python 教程", "https://a.com/1")]),
                     FailingProvider()])
    r = e.search("Python 教程", QueryOptions())
    assert r.status == Status.PARTIAL
    assert r.ok is True
    assert len(r.results) == 1
    failed = [p for p in r.providers if not p.ok]
    assert failed and failed[0].status == "error"


def test_status_no_results_is_explicit():
    e = make_engine([StaticProvider("s1", [])])
    r = e.search("不可能存在的查询词", QueryOptions())
    assert r.status == Status.NO_RESULTS
    assert r.ok is False
    assert "未找到" in r.message
    assert r.results == []


def test_status_error_when_all_sources_fail():
    e = make_engine([FailingProvider(),
                     FailingProvider(UpstreamError("boom", source="x"))])
    r = e.search("任意", QueryOptions())
    assert r.status == Status.ERROR
    assert r.ok is False
    assert r.providers and all(not p.ok for p in r.providers)


def test_empty_query_returns_error_without_crash():
    e = make_engine([StaticProvider("s1", [])])
    r = e.search("", QueryOptions())
    assert r.status == Status.ERROR and "空" in r.message


def test_unexpected_exception_is_contained():
    class Boom(StaticProvider):
        def search(self, q, o, c):
            raise RuntimeError("未预期的崩溃")

    e = make_engine([Boom("boom", [])])
    r = e.search("x", QueryOptions())
    assert r.status == Status.ERROR
    assert "RuntimeError" in r.message


# ---------------------------------------------------------------- 限流
def test_rate_limited_returns_status():
    cfg = Config(cache_enabled=False, rate_limit_enabled=True,
                 global_qps=1.0, global_burst=1, audit_log=False)
    e = make_engine([StaticProvider("s1", [mk("T", "https://a.com/1")])], cfg=cfg)
    e.search("a", QueryOptions())
    r = e.search("b", QueryOptions())
    assert r.status == Status.ERROR and "频繁" in r.message


# ---------------------------------------------------------------- 缓存
def test_cache_hit_avoids_provider_call():
    cache = Cache(path=":memory:", enabled=True, ttl=600)
    p = StaticProvider("s1", [mk("Python 教程", "https://a.com/1")])
    e = make_engine([p], cache=cache)
    e.search("Python 教程", QueryOptions(use_cache=True))
    n_after_first = len(p.calls)
    r2 = e.search("Python 教程", QueryOptions(use_cache=True))
    assert len(p.calls) == n_after_first, "第二次应命中缓存, 不再访问检索源"
    assert r2.cached is True
    assert len(r2.results) == 1


def test_disabled_cache_always_calls_provider():
    p = StaticProvider("s1", [mk("Python 教程", "https://a.com/1")])
    e = make_engine([p], cache=Cache(path=":memory:", enabled=False))
    e.search("Python 教程", QueryOptions(use_cache=False))
    n_after_first = len(p.calls)
    e.search("Python 教程", QueryOptions(use_cache=False))
    assert len(p.calls) > n_after_first, "关闭缓存后每次都应真正检索"


def test_failed_result_is_not_cached():
    cache = Cache(path=":memory:", enabled=True)
    e = make_engine([FailingProvider()], cache=cache)
    e.search("x", QueryOptions(use_cache=True))
    assert cache.get(QueryOptions().cache_key("x"), "search") is None


# ---------------------------------------------------------------- 输出
def test_response_has_required_fields():
    e = make_engine([StaticProvider("s1", [mk("T", "https://a.com/1", "摘要")])])
    r = e.search("T", QueryOptions())
    d = r.results[0].to_ai_dict()
    for f in ("title", "url", "publish_time", "source_type",
              "snippet", "relevance_score"):
        assert f in d


def test_to_context_includes_numbered_citations():
    e = make_engine([StaticProvider("s1", [mk("T1", "https://a.com/1", "摘要一"),
                                           mk("T2", "https://b.com/2", "摘要二")])])
    r = e.search("T", QueryOptions())
    ctx = r.to_context()
    assert "[1]" in ctx and "[2]" in ctx
    assert "https://a.com/1" in ctx


def test_to_json_is_parseable():
    import json
    e = make_engine([StaticProvider("s1", [mk("T", "https://a.com/1")])])
    r = e.search("T", QueryOptions())
    d = json.loads(r.to_json())
    assert d["status"] == "ok" and d["results"]


def test_compression_respects_budget():
    from intellisearch.ai.formatter import compress
    payload = {"results": [{"title": "T" * 40, "url": "https://a.com",
                            "snippet": "S" * 200, "clean_text": "C" * 500}
                           for _ in range(10)]}
    out = compress(payload, 800)
    import json
    assert len(json.dumps(out, ensure_ascii=False)) <= 900


# ---------------------------------------------------------------- 会话
def test_session_context_resolves_reference():
    store = SessionStore()
    store.remember("s1", "铭凡 UM880 Pro 的 NPU 算力", ["铭凡", "UM880 Pro"])
    q = apply_context("它的价格呢", "s1", store)
    assert "铭凡" in q or "UM880" in q


def test_session_context_ignored_for_clear_query():
    store = SessionStore()
    store.remember("s1", "铭凡 UM880", ["铭凡"])
    assert apply_context("Python 列表推导式", "s1", store) == "Python 列表推导式"


def test_session_memory_recorded_after_search():
    e = make_engine([StaticProvider("s1", [mk("铭凡 UM880 Pro 参数",
                                              "https://a.com/1")])])
    r = e.search("铭凡 UM880 Pro 参数", QueryOptions(session_id="sess-1"))
    assert e.sessions.last_query("sess-1") != ""


# ---------------------------------------------------------------- 冲突
def test_conflict_detected_when_values_differ():
    rs = [mk("A", "https://a.com/1", "NPU 算力 50 TOPS", source="s1"),
          mk("B", "https://b.com/2", "NPU 算力 48 TOPS", source="s2")]
    conflicts = detect_conflicts(rs, ["NPU", "算力"])
    assert conflicts
    assert "差异" in conflicts[0]["message"]


def test_no_conflict_when_values_agree():
    rs = [mk("A", "https://a.com/1", "NPU 算力 50 TOPS", source="s1"),
          mk("B", "https://b.com/2", "NPU 算力 50 TOPS", source="s2")]
    assert detect_conflicts(rs, ["NPU", "算力"]) == []


def test_facts_filtered_by_relevance():
    facts = extract_facts("价格 3999 元，算力 50 TOPS", ["算力"])
    assert all("算力" in k or "tops" in k for k, _, _ in facts)


# ---------------------------------------------------------------- 上限
def test_top_k_is_enforced():
    many = [mk(f"T{i}", f"https://a.com/{i}") for i in range(30)]
    e = make_engine([StaticProvider("s1", many)])
    r = e.search("T", QueryOptions(top_k=3))
    assert len(r.results) == 3
