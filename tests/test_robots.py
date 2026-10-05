"""robots.txt 解析与访问判定测试。"""
from intellisearch.http.robots import RobotsCache, _pattern_match


def test_pattern_match_prefix():
    assert _pattern_match("/admin", "/admin/page")
    assert not _pattern_match("/admin", "/public")


def test_pattern_match_wildcard_and_anchor():
    assert _pattern_match("/*.pdf$", "/a/b.pdf")
    assert not _pattern_match("/*.pdf$", "/a/b.pdf.html")


def test_allow_wins_on_same_length():
    txt = ("User-agent: *\n"
           "Disallow: /private\n"
           "Allow: /private/public\n")
    rc = RobotsCache(fetcher=lambda u: None)
    rc._cache["https://x.com"] = (__import__("time").time(), rc._parse(txt))
    allowed, _ = rc.check("https://x.com/private/public")
    assert allowed is True


def test_disallow_blocks():
    txt = "User-agent: *\nDisallow: /admin\n"
    rc = RobotsCache(fetcher=lambda u: None)
    rc._cache["https://x.com"] = (__import__("time").time(), rc._parse(txt))
    allowed, _ = rc.check("https://x.com/admin/x")
    assert allowed is False


def test_specific_agent_preferred():
    txt = ("User-agent: *\nDisallow: /\n\n"
           "User-agent: mybot\nAllow: /\n")
    rc = RobotsCache(fetcher=lambda u: None, user_agent="mybot")
    rc._cache["https://x.com"] = (__import__("time").time(), rc._parse(txt))
    assert rc.check("https://x.com/anything")[0] is True


def test_crawl_delay_parsed():
    txt = "User-agent: *\nDisallow: /x\nCrawl-delay: 2.5\n"
    rc = RobotsCache(fetcher=lambda u: None)
    rc._cache["https://x.com"] = (__import__("time").time(), rc._parse(txt))
    _, delay = rc.check("https://x.com/ok")
    assert delay == 2.5


def test_missing_robots_allows_access():
    class R:
        status_code = 404
        text = ""
    rc = RobotsCache(fetcher=lambda u: R())
    assert rc.check("https://unknown.example/page")[0] is True


def test_offline_mode_allows():
    rc = RobotsCache(fetcher=None)
    assert rc.check("https://any.example/x")[0] is True
