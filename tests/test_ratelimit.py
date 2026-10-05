"""限流与安全过滤测试。"""
import time

import pytest

from intellisearch.errors import RateLimitError
from intellisearch.models import SearchResult
from intellisearch.safety.blacklist import SafetyGuard
from intellisearch.safety.ratelimit import RateLimiter, TokenBucket


def test_token_bucket_allows_burst_then_blocks():
    b = TokenBucket(rate=1.0, burst=2)
    assert b.consume() and b.consume()
    assert not b.consume()


def test_token_bucket_refills_over_time():
    b = TokenBucket(rate=50.0, burst=1)
    assert b.consume()
    time.sleep(0.12)
    assert b.consume()


def test_rate_limiter_global_limit():
    rl = RateLimiter(global_qps=2.0, global_burst=2)
    rl.acquire()
    rl.acquire()
    with pytest.raises(RateLimitError):
        rl.acquire()


def test_rate_limiter_per_session():
    rl = RateLimiter(global_qps=100, global_burst=100,
                     session_qps=1.0, session_burst=1)
    rl.acquire("s1")
    with pytest.raises(RateLimitError):
        rl.acquire("s1")
    rl.acquire("s2")          # 不同会话互不影响


def test_rate_limiter_disabled():
    rl = RateLimiter(enabled=False, global_qps=0.01, global_burst=1)
    for _ in range(10):
        rl.acquire()          # 不应抛错


def test_host_delay_is_enforced():
    rl = RateLimiter(enabled=True)
    t0 = time.monotonic()
    rl.wait_host("https://a.com/x", crawl_delay=0.15)
    rl.wait_host("https://a.com/y", crawl_delay=0.15)
    assert time.monotonic() - t0 >= 0.14


# ---------------------------------------------------------------- 安全
def mk(title, url, snippet=""):
    return SearchResult(title=title, url=url, snippet=snippet)


def test_blacklist_query_blocked():
    from intellisearch.errors import SafetyBlocked
    g = SafetyGuard(blacklist=["违规词"])
    with pytest.raises(SafetyBlocked):
        g.check_query("帮我查 违规词 的资料")


def test_blacklist_result_filtered():
    g = SafetyGuard(blacklist=["违规词"])
    kept, dropped = g.filter_results([mk("含违规词的页面", "https://a.com/1"),
                                      mk("正常页面", "https://b.com/2")])
    assert dropped == 1 and len(kept) == 1


def test_suspicious_domain_blocked():
    g = SafetyGuard()
    assert g.is_suspicious("http://1.2.3.4/login") is True
    assert g.is_suspicious("https://github.com/x") is False


def test_paywall_marked_not_dropped():
    g = SafetyGuard(skip_paywall=True)
    r = mk("付费内容", "https://a.com/1", snippet="该内容需要登录后查看")
    kept, _ = g.filter_results([r])
    assert len(kept) == 1
    assert kept[0].extra.get("paywall") is True


def test_empty_blacklist_allows_everything():
    g = SafetyGuard(blacklist=[])
    g.check_query("任何内容")
    kept, dropped = g.filter_results([mk("a", "https://a.com/1")])
    assert dropped == 0
