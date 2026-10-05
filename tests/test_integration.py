"""集成测试 —— 依赖真实网络, 默认跳过。

运行::

    pytest --run-integration -v
"""
import time

import pytest

from intellisearch.config import Config
from intellisearch.engine import SearchEngine
from intellisearch.models import Freshness, QueryOptions, Status

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine():
    cfg = Config(cache_enabled=False, rate_limit_enabled=False, audit_log=False)
    e = SearchEngine(config=cfg)
    yield e
    e.close()


@pytest.fixture(scope="module")
def chinese(engine):
    """中文查询只检索一次, 供"正确性"与"上游可用性"两组断言共用。"""
    return engine.search("铭凡 UM880 Pro 的 NPU 算力多少",
                         QueryOptions(top_k=5, timeout=20))


def _assert_relevant(r, terms):
    """交给调用方的每一条结果都必须与查询有关(反爬软封闸门的下游保证)。"""
    for x in r.results:
        hay = f"{x.title} {x.snippet} {x.url}".lower()
        assert any(t.lower() in hay for t in terms), \
            f"把无关结果交给了调用方: {x.title} / {x.url}"


def test_real_search_chinese_is_correct(chinese):
    """与上游可用性无关的正确性: 状态合法、逐源有状态、结果相关、字段完整。"""
    assert chinese.status in (Status.OK, Status.PARTIAL, Status.NO_RESULTS), \
        chinese.message
    assert chinese.message, "无论有无结果都必须给出说明"
    assert chinese.providers, "应上报各检索源状态"
    for p in chinese.providers:
        assert p.status
        if p.status in ("error", "irrelevant"):
            assert p.error, f"{p.name} 状态 {p.status} 未给原因"
    _assert_relevant(chinese, ["铭凡", "UM880", "NPU"])
    for x in chinese.results:
        assert x.title and x.url.startswith("http")


@pytest.mark.xfail(strict=False,
                reason="上游可能反爬(403/验证码)或软封(整批结果被替换), 属外部因素; "
                       "此时闸门会拒绝无关结果, 可用结果不足 3 条是正确行为")
def test_real_search_chinese_yields_enough(chinese):
    """上游可用性探针: 正常情况下中文查询应能拿到 ≥3 条结果。"""
    assert len(chinese.results) >= 3, f"结果过少: {len(chinese.results)}"


def test_real_search_english_is_correct(engine):
    r = engine.search("Python list comprehension official documentation",
                      QueryOptions(top_k=5, timeout=20, lang="en"))
    assert r.status in (Status.OK, Status.PARTIAL, Status.NO_RESULTS), r.message
    _assert_relevant(r, ["python", "comprehension"])
    if r.results:
        assert all(x.url.startswith("http") and x.title for x in r.results)


@pytest.mark.xfail(strict=False, reason="上游反爬/软封时可能拿不到结果, 属外部因素")
def test_real_search_english_yields_results(engine):
    r = engine.search("Python list comprehension official documentation",
                      QueryOptions(top_k=5, timeout=20, lang="en"))
    assert r.results, r.message
    assert any("python" in x.url.lower() or "python" in x.title.lower()
               for x in r.results)


@pytest.mark.xfail(strict=False,
                reason="上游可能反爬(403/验证码)或软封(返回整批无关内容), 属外部因素; "
                       "软封时 guard 会把结果判为 irrelevant, 这里预期失败而不是全绿")
def test_all_providers_reachable(engine):
    """至少应有 2 个独立检索源可用(否则谈不上多源融合)。"""
    r = engine.search("AMD Zen5", QueryOptions(top_k=5, timeout=20))
    ok = [p for p in r.providers if p.ok and p.count > 0]
    assert len(ok) >= 2, [p.to_dict() for p in r.providers]


def test_multi_source_plumbing_intact(engine):
    """与上游内容无关的不变量: 多个源都给出了明确状态(而非静默无响应)。"""
    r = engine.search("AMD Zen5", QueryOptions(top_k=5, timeout=20))
    assert len(r.providers) >= 2
    for p in r.providers:
        assert p.status, f"{p.name} 未上报状态"
        if p.status in ("error", "irrelevant"):
            assert p.error, f"{p.name} 状态为 {p.status} 却没给原因"


def test_irrelevant_results_never_reach_caller(engine):
    """反爬软封时, 引擎可以返回"无结果", 但绝不能把无关链接当检索结果给出去。

    实测过 Bing 被限流后返回 200 + 结构完整的无关结果页(热门新闻/促销),
    解析器能解析、计数为 10, 但对调用方是有害的。
    """
    r = engine.search("AMD Zen5 架构", QueryOptions(top_k=5, timeout=20))
    if not any(p.status == "irrelevant" for p in r.providers):
        pytest.skip("当前没有被软封的源, 该场景无法构造")
    terms = [t for t in ("AMD", "Zen5", "架构") if t]
    for x in r.results:
        hay = f"{x.title} {x.snippet} {x.url}".lower()
        assert any(t.lower() in hay for t in terms), \
            f"把无关结果交给了调用方: {x.title} / {x.url}"


def test_site_filter_end_to_end(engine):
    r = engine.search("python asyncio", QueryOptions(
        top_k=5, site="docs.python.org", timeout=20))
    assert r.status in (Status.OK, Status.PARTIAL)
    if r.results:
        assert all("docs.python.org" in x.url for x in r.results)


def test_freshness_filter_end_to_end(engine):
    r = engine.search("AI 芯片 发布", QueryOptions(
        top_k=5, freshness=Freshness.WEEK, timeout=20))
    assert r.status in (Status.OK, Status.PARTIAL, Status.NO_RESULTS)


def test_content_fetch_end_to_end(engine):
    r = engine.search("Python 列表推导式 教程", QueryOptions(
        top_k=2, fetch_content=True, max_content_chars=1500, timeout=25))
    assert r.status in (Status.OK, Status.PARTIAL)
    fetched = [x for x in r.results if x.clean_text]
    assert fetched, "至少应抓到一篇正文"
    assert any(len(x.clean_text) > 100 for x in fetched)


def test_repeated_search_stability(engine):
    """连续多次检索: 验证稳定性与无累积故障。"""
    ok_count = 0
    for i, q in enumerate(["Python 教程", "Rust 异步", "AMD 处理器"]):
        r = engine.search(q, QueryOptions(top_k=3, timeout=20))
        if r.status in (Status.OK, Status.PARTIAL) and r.results:
            ok_count += 1
        time.sleep(0.4)
    assert ok_count >= 2, f"成功率过低: {ok_count}/3"


def test_no_results_returns_clear_status(engine):
    r = engine.search("zzqxwvbn 不存在的随机查询词 jklsdf",
                      QueryOptions(top_k=3, timeout=20))
    assert r.status in (Status.NO_RESULTS, Status.OK, Status.PARTIAL)
    assert r.message, "无论有无结果都必须返回说明信息"


def test_timeout_is_respected(engine):
    t0 = time.time()
    r = engine.search("Python", QueryOptions(top_k=3, timeout=1, retries=0))
    elapsed = time.time() - t0
    assert elapsed < 30, f"超时未生效, 耗时 {elapsed:.1f}s"
    assert isinstance(r.status, Status)
