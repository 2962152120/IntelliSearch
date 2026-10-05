"""本轮修复的回归锁定测试(每条都经过"反向验证": 去掉修复即失败)。

覆盖 5 个真实缺陷:
  R1 query.py  —— CJK_SPLIT_CHARS 把词内字当切分点, "为啥"被切成"啥"
  R2 fusion.py —— 闸门 any(_hit_terms) 只需 1 条命中残片词即放行整批
  R3 engine.py —— engine.fetch 只看 bool(r.text), 404/验证页被判成功
  R4 extractor —— 段落数因子无上限, 导航容器压过正文
  R5 engine.py —— engine.fetch 丢弃 robots crawl_delay(与 _fetch_contents 不一致)
"""
import hashlib
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from intellisearch.config import Config
from intellisearch.engine import SearchEngine
from intellisearch.extract.extractor import extract as extract_page
from intellisearch.http.fetcher import FetchOutcome
from intellisearch.models import (ParsedQuery, QueryOptions, SearchResult,
                                  now_utc)
from intellisearch.retrieval.fusion import (_drop_irrelevant,
                                            parallel_search)
from intellisearch.retrieval.query import core_terms, tokenize

from conftest import StaticProvider


def mk(title, url, snippet=""):
    return SearchResult(title=title, url=url, snippet=snippet)


def make_engine(outcome, robots_delay=0.0, limiter=None):
    cfg = Config()
    cfg.cache_enabled = False
    cfg.providers = ["bing_rss"]
    cfg.rate_limit_enabled = False
    eng = SearchEngine(cfg)
    eng.cache = _NoCache()
    eng.fetcher = _StubFetcher(outcome)
    eng._robots = _StubRobots(robots_delay)
    if limiter is not None:
        eng.limiter = limiter
    return eng


class _StubFetcher:
    def __init__(self, outcome):
        self._o = outcome

    def fetch(self, url, mode="auto", **kw):
        return self._o

    def render_engine(self):
        return "stub"


class _StubRobots:
    def __init__(self, delay=0.0):
        self.delay = delay

    def check(self, url):
        return True, self.delay


class _NoCache:
    enabled = False

    def get(self, *a, **kw):
        return None

    def set(self, *a, **kw):
        pass


class _SpyLimiter:
    def __init__(self):
        self.calls = []

    def wait_host(self, url, crawl_delay=None):
        self.calls.append(crawl_delay)


# ----------------------------------------------------------------------
# R1  分词: 词内字不得成为切分点
# ----------------------------------------------------------------------
@pytest.mark.parametrize("word", ["人工智能", "最新", "性能", "可能",
                                   "一般", "请求", "北京"])
def test_single_char_stopwords_do_not_shred_words(word):
    """CJK_SPLIT_CHARS 不能把 能/最/为/可/一/些/请 这类词内字当切分点。

    修复前: 人工智能->人工智, 最新->新, 为啥->啥, 请求->[] (整词消失)。
    注意: 纯疑问词(为啥/怎么样/一些)现在被**整体丢弃**是正确行为,
    它们不是检索实体, 断言它们出现在 tokens 里反而会固化错误。
    """
    toks = tokenize(word)
    assert word in toks, f"{word} 被切碎成 {toks}"


@pytest.mark.parametrize("q", ["为啥", "怎么样", "一些", "是什么"])
def test_pure_question_words_are_dropped_not_fragmented(q):
    """纯疑问词整体丢弃, 不能碎成 '为什'/'么选择' 这类伪词。

    伪词长度 >=2 会被 fusion._strong_terms 当实词, 让"为什的拼音"这类
    词典页通过相关性闸门。
    """
    from intellisearch.retrieval.query import _split_cjk
    assert _split_cjk(q) == [], f"{q} 被切成 {_split_cjk(q)}"


def test_question_query_keeps_meaningful_terms():
    """实测假成功场景: 查询词里不能出现 '啥' 这种无意义残片。"""
    terms = core_terms("为啥我的 fastapi 请求一并发就502")
    assert "啥" not in terms, f"仍混入单字残片: {terms}"
    assert "fastapi" in terms and "502" in terms, terms


def test_split_cjk_never_emits_single_char_fragments():
    """切分器只应产出 >=2 字片段(单字对检索无价值且污染闸门)。"""
    from intellisearch.retrieval.query import _split_cjk
    for seg in ("为啥我的", "怎么样", "算力多少", "请问Python", "这个新型发动机"):
        for part in _split_cjk(seg):
            assert len(part) >= 2, f"{seg} -> 单字碎片 {part!r}"


def test_real_words_survive_end_to_end():
    p = core_terms("这个新型发动机的能耗怎么样")
    assert "发动机" in " ".join(p), p


# ----------------------------------------------------------------------
# R2  相关性闸门: 残片词不得放行整批
# ----------------------------------------------------------------------
def test_dictionary_pages_for_fragment_term_are_dropped():
    """真实假成功: '啥'字字典页曾因命中残片词 '啥' 而整批通过闸门。"""
    terms = core_terms("为啥我的 fastapi 请求一并发就502")
    junk = [mk("啥的意思", "https://www.zdic.net/hans/%E5%92%80"),
            mk("啥字怎么读", "https://dict.baidu.com/s?wd=%E5%92%80"),
            mk("啥时候的用法", "https://www.baidu.com/s?wd=%E5%92%80")]
    kept, why = _drop_irrelevant(junk, terms)
    assert kept == [], "与查询无关的字典页必须丢弃"
    assert "无关" in why


def test_weak_question_terms_alone_do_not_trigger_dropping():
    """检索词全是疑问词时**不做相关性判定**(而非用弱词硬匹配)。

    权衡: 此时没有任何可用于判断的实词, 若拿"什么/怎么"去硬匹配,
    几乎任何页面都会命中 -> 闸门形同虚设; 若强行判无关则会误杀真结果。
    因此选择"不过滤": 宁可放过, 也不误杀。
    """
    items = [mk("什么东西", "https://a.com/1")]
    kept, why = _drop_irrelevant(items, ["什么", "怎么"])
    assert len(kept) == 1 and why == ""


def test_strong_terms_take_precedence_over_weak_ones():
    """有实词时只用实词判定, 疑问词命中不再算证据。"""
    items = [mk("什么东西都有", "https://a.com/1")]
    kept, why = _drop_irrelevant(items, ["fastapi", "什么"])
    assert kept == [], "只命中'什么'不能证明与 fastapi 相关"
    assert "无关" in why


def test_relevant_results_still_pass():
    items = [mk("FastAPI 并发 502 排查指南", "https://a.com/1",
                "uvicorn worker 并发导致 502"),
             mk("FastAPI 502 错误排查", "https://b.com/2",
                "并发请求返回 502 的原因")]
    terms = core_terms("为啥我的 fastapi 请求一并发就502")
    kept, why = _drop_irrelevant(items, terms)
    assert len(kept) == 2 and why == ""


# ----------------------------------------------------------------------
# R2b闸门必须逐条过滤, 不能 1 条命中就放行整批
# ----------------------------------------------------------------------
JUNK_TITLES = ["伊朗局势时间线", "京东双十一活动", "百度一下你就知道",
               "Docker 入门教程", "天猫超市", "华为新品发布"]


def _junk_batch(n=6):
    return [mk(t, f"https://junk{i}.com/{i}") for i, t in enumerate(JUNK_TITLES[:n])]


@pytest.mark.parametrize("query", ["FastAPI 并发 502", "AMD Zen5 架构",
                                   "人工智能发展趋势"])
def test_pure_junk_batch_always_dropped(query):
    kept, why = _drop_irrelevant(_junk_batch(), core_terms(query))
    assert kept == [], "整批垃圾必须丢弃"


def test_one_relevant_result_does_not_rescue_junk_batch():
    """修复前: 1 条命中即放行整批, 5 条垃圾跟着一起进大模型上下文。

    这是比"返回空"更糟的假成功 —— 垃圾链接会被当证据引用。
    修复后: 逐条过滤, 只留下真正相关的那 1 条。
    """
    batch = [mk("FastAPI 并发 502 排查", "https://a.com/1",
                "uvicorn worker 并发 502")] + _junk_batch(5)
    kept, why = _drop_irrelevant(batch, core_terms("FastAPI 并发 502"))
    assert len(kept) == 1, "只应保留唯一那条真结果, 不能连带 5 条垃圾"
    assert kept[0].url == "https://a.com/1"


def test_majority_relevant_batch_is_kept():
    """多条相关结果必须都保留 —— 不能把闸门做成"太严导致查不到东西"。"""
    items = [mk("FastAPI 并发 502 排查", "https://a.com/1", "并发 502"),
             mk("FastAPI 502 错误原因", "https://b.com/2", "并发请求 502"),
             mk("解决 502 Bad Gateway", "https://c.com/3", "FastAPI 并发 502")]
    kept, why = _drop_irrelevant(items, core_terms("FastAPI 并发 502"))
    assert len(kept) == 3 and why == ""


def test_authoritative_domain_with_single_term_hit_is_kept():
    """官方文档站即使只命中 1 个实词也应保留 —— 官网首页本身有检索价值。"""
    items = [mk("FastAPI 文档", "https://fastapi.tiangolo.com/zh/", "高性能 Python 框架")]
    kept, why = _drop_irrelevant(items, core_terms("FastAPI 并发 502"))
    assert len(kept) == 1 and why == ""


def test_multi_char_question_words_do_not_become_strong_terms():
    """'为什么'不能被切成 '为什'/'么选择' —— 伪词会被当实词放进闸门。"""
    from intellisearch.retrieval.fusion import _strong_terms
    for q, banned in (("为什么选择Python", ("为什", "么选择")),
                      ("有没有便宜的显卡", ("有没",)),
                      ("是不是免费", ("是不", "不是"))):
        terms = core_terms(q)
        for b in banned:
            assert b not in terms, f"{q} 产出伪词 {b}: {terms}"
        assert _strong_terms(terms), f"{q} 应保留实词"


def test_request_word_is_not_destroyed():
    """'请求' 曾因 '请' 被当作段首虚词而整词消失(tokenize 返回 [])。

    修复后可以带上后缀成词("请求超时"), 只要不丢"请求"这个语义即可。
    """
    for q in ("请求超时怎么办", "我的请求失败", "请求"):
        toks = tokenize(q)
        assert toks, f"{q} 被整词丢弃"
        assert any("请求" in t for t in toks), f"{q} -> {toks}"


@pytest.mark.parametrize("word", [
    # 这些词的首字都是虚词/切分字。任何"丢弃段首字"的启发式都会把它们
    # 整词吃掉 —— 实测曾有 16 个常用词里 15 个被切坏。
    "并发", "和平", "在线", "是非", "给力", "等于", "及时", "比较",
    "被子", "着急", "并且", "在家", "请求", "看着",
    # 词内字是切分点时也不能腰斩
    "人工智能", "最新", "性能", "可能", "北京天气",
])
def test_real_words_are_never_destroyed(word):
    """分词器不得丢失用户输入里的实词。

    这条是"检索结果是否正常"的地基: 查询词被切烂, 检索源收到的就是
    一个错的 query, 后面所有排序/相关性判断都无从谈起。
    """
    assert word in tokenize(word), f"{word} 被切碎成 {tokenize(word)}"


def test_concurrency_word_survives_realistic_query():
    """实测回归: 查询 'FastAPI 并发 502' 的 '并发' 曾整词消失。"""
    terms = core_terms("为啥我的 fastapi 请求一并发就502")
    joined = " ".join(terms).lower()
    assert "并发" in joined, f"查询词丢了'并发': {terms}"
    assert "502" in joined and "fastapi" in joined, terms


def test_measure_word_does_not_lead_a_pseudo_term():
    """助词收尾不应产出 '的显卡' 这种伪词。"""
    terms = core_terms("有没有便宜的显卡")
    assert not any(t.startswith("的") for t in terms), terms
    assert "显卡" in " ".join(terms), terms


# ----------------------------------------------------------------------
# R3  engine.fetch: 4xx / 空正文不得判成功
# ----------------------------------------------------------------------
def test_http_404_page_is_not_reported_as_success():
    out = make_engine(FetchOutcome(
        url="http://x.cn/gone", status=404,
        text="<html><body><h1>404 Not Found</h1></body></html>",
        fetch_mode="http", reason="http_status")).fetch("http://x.cn/gone", mode="http")
    assert out["ok"] is False, "404 页有正文, 但它不是内容"
    assert out["status"] != "ok"
    assert out["http_status"] == 404


def test_403_anti_crawl_page_is_not_reported_as_success():
    out = make_engine(FetchOutcome(
        url="http://x.cn/w", status=403,
        text="<html><body>请完成安全验证</body></html>",
        fetch_mode="http", reason="http_status")).fetch("http://x.cn/w", mode="http")
    assert out["ok"] is False
    assert out["http_status"] == 403


def test_empty_body_is_not_reported_as_success():
    out = make_engine(FetchOutcome(url="http://x.cn/e", status=200, text="",
                                   fetch_mode="http")).fetch("http://x.cn/e", mode="http")
    assert out["ok"] is False


def test_real_200_content_still_succeeds():
    body = "<html><body><article><p>" + ("FastAPI 并发性能实践指南。" * 12) + \
           "</p></article></body></html>"
    out = make_engine(FetchOutcome(url="http://x.cn/ok", status=200, text=body,
                                   fetch_mode="http")).fetch("http://x.cn/ok", mode="http")
    assert out["ok"] is True and out["status"] == "ok"
    assert "FastAPI" in out["clean_text"]


# ----------------------------------------------------------------------
# R4  正文抽取: 段落因子封顶, 导航不得压过正文
# ----------------------------------------------------------------------
NAV_PAGE = ("<html><body>"
            "<div id=\"top-nav-bar\">" + "<p>Tutorials</p>" * 140 + "</div>"
            "<div id=\"main\">" + ("<p>%s</p>" % (
                "FastAPI 是一个基于标准类型注解的 Python Web 框架, 性能接近 "
                "Node.js 与 Go, 并且能自动生成交互式 API 文档。" * 2)) * 4 +
            "</div></body></html>")


def test_many_paragraph_nav_does_not_beat_article_body():
    """修复前 #top-nav-bar(p=140) 得分 406.9 > #main(p=4) 258.8。

    段落数因子不封顶时, 导航容器的 ×17.8 会碾压正文的 ×1.48。
    """
    page = extract_page(NAV_PAGE, url="http://w3schools.test/x", max_chars=6000)
    assert "FastAPI" in page.clean_text, "正文被导航容器挤掉"
    assert "Tutorials" not in page.clean_text


def test_paragraph_factor_is_capped():
    """同样的总文本, 段落数多不应让得分无上限地膨胀。

    不封顶时 (1 + p*0.12): 140 段 -> ×17.8, 而 1 段 -> ×1.12,
    差 15 倍以上, 足以让导航容器碾压正文。封顶到 2.0 后差距收敛。
    """
    import intellisearch.extract.extractor as E
    from intellisearch.extract.dom import parse_html

    body = "正文内容" * 200          # 固定总长度, 只改段落切分

    def score(p_count):
        per = len(body) // p_count
        chunks = [body[i * per:(i + 1) * per].ljust(per, "文")
                  for i in range(p_count)]
        html = "<html><body><div id='c'>" + \
               "".join("<p>%s</p>" % c for c in chunks) + "</div></body></html>"
        node = parse_html(html).find("div")
        s = E._score_node(node)
        assert s > 0, "打分不应为 0(文本过短会走另一分支)"
        return s

    one = score(1)
    many = score(140)
    # 封顶后段落因子最大 2.0, 且 avg_p 变小会压低 p_bonus,
    # 实际差距应远小于不封顶时的 15 倍
    assert many / one < 4.0, f"140 段得分 {many:.1f} 相对 1 段 {one:.1f} 过高"


# ----------------------------------------------------------------------
# R5  engine.fetch 必须遵守 Crawl-delay
# ----------------------------------------------------------------------
def test_fetch_respects_crawl_delay():
    limiter = _SpyLimiter()
    make_engine(FetchOutcome(url="http://a.cn/x", status=200,
                             text="<p>x</p>" * 200, fetch_mode="http"),
                robots_delay=7.0, limiter=limiter).fetch("http://a.cn/x", mode="http")
    assert limiter.calls == [7.0], f"crawl-delay 未生效: {limiter.calls}"


def test_no_delay_no_wait_call():
    limiter = _SpyLimiter()
    make_engine(FetchOutcome(url="http://a.cn/x", status=200,
                             text="<p>x</p>" * 200, fetch_mode="http"),
                robots_delay=0.0, limiter=limiter).fetch("http://a.cn/x", mode="http")
    assert limiter.calls == []


def test_robots_disallowed_still_short_circuits():
    eng = make_engine(FetchOutcome(url="http://a.cn/x", status=200, text="<p>x</p>" * 200))
    eng._robots = type("R", (), {"check": lambda self, u: (False, 0.0)})()
    out = eng.fetch("http://a.cn/x", mode="http")
    assert out["ok"] is False and out["status"] == "robots_disallowed"


# ----------------------------------------------------------------------
# R3b  _fetch_contents 必须与 fetch() 用同一套 ok 判定
# ----------------------------------------------------------------------
def test_fetch_contents_rejects_404_body():
    """search(fetch_content=True) 走的另一条路径, 此前不校验 ok,
    404 正文会被抽成 clean_text 直接作为引用喂给大模型。
    """
    r404 = FetchOutcome(
        url="http://x.cn/gone", status=404,
        text="<html><body><h1>404 该页面不存在</h1>" + "填充内容。" * 60 +
             "</body></html>", fetch_mode="http")
    eng = make_engine(r404, limiter=_SpyLimiter())
    res = mk("t", "http://x.cn/gone")
    eng._fetch_contents([res], _opts(fetch_content=True), None)
    assert res.extra.get("fetch_status") == "http_404"
    assert res.clean_text == "", "404 正文不得进入 clean_text"


def test_fetch_contents_keeps_normal_body():
    body = "<html><body><article><p>" + ("FastAPI 并发性能实践指南。" * 12) + \
           "</p></article></body></html>"
    eng = make_engine(FetchOutcome(url="http://a.cn/ok", status=200, text=body,
                                   fetch_mode="http"), limiter=_SpyLimiter())
    res = mk("t", "http://a.cn/ok")
    eng._fetch_contents([res], _opts(fetch_content=True), None)
    assert res.extra.get("fetch_status") == "http"
    assert "FastAPI" in res.clean_text


def _opts(**kw):
    kw.setdefault("top_k", 1)
    return QueryOptions(**kw)


# ----------------------------------------------------------------------
# R3c  失败不得写缓存(瞬时故障不能固化)
# ----------------------------------------------------------------------
class _SpyCache:
    enabled = True

    def __init__(self, hit=None):
        self.writes = []
        self._hit = hit
        self.gets = 0

    def get(self, *a, **kw):
        self.gets += 1
        return self._hit

    def set(self, ck, value, kind, ttl=None):
        self.writes.append((kind, value.get("ok")))


def test_failure_is_not_cached():
    sc = _SpyCache()
    eng = make_engine(FetchOutcome(url="http://x.cn/g", status=404, text="<p>x</p>" * 50,
                                   fetch_mode="http"))
    eng.cache = sc
    out = eng.fetch("http://x.cn/g", mode="http")
    assert out["ok"] is False
    assert sc.writes == [], f"失败结果不应缓存(会把瞬时故障固化 ttl*4): {sc.writes}"


def test_success_is_still_cached():
    sc = _SpyCache()
    body = "<html><body><article><p>" + ("正常内容。" * 40) + "</p></article></body></html>"
    eng = make_engine(FetchOutcome(url="http://a.cn/ok", status=200, text=body,
                                   fetch_mode="http"))
    eng.cache = sc
    assert eng.fetch("http://a.cn/ok", mode="http")["ok"] is True
    assert sc.writes, "成功结果必须缓存, 否则缓存形同虚设"


# ----------------------------------------------------------------------
# R3d  失败分支的 key 契约必须与成功分支一致
# ----------------------------------------------------------------------
@pytest.mark.parametrize("scenario", ["robots", "error", "http404"])
def test_failure_branches_expose_same_keys(scenario):
    """MCP _tool_fetch 直接把 dict 序列化给模型, 结构不一致会让下游
    写 out["clean_text"] 时 KeyError。
    """
    body = "<html><body><article><p>" + ("正常内容。" * 40) + "</p></article></body></html>"
    eng = make_engine(FetchOutcome(url="http://a.cn/x", status=200, text=body,
                                   fetch_mode="http"))
    if scenario == "robots":
        eng._robots = type("R", (), {"check": lambda s, u: (False, 0.0)})()
    elif scenario == "error":
        class Boom:
            def fetch(s, *a, **k):
                raise RuntimeError("boom")

            def render_engine(s):
                return "stub"
        eng.fetcher = Boom()
    else:
        eng.fetcher = _StubFetcher(FetchOutcome(
            url="http://a.cn/x", status=404, text="<p>x</p>" * 50, fetch_mode="http"))
    out = eng.fetch("http://a.cn/x", mode="http")
    for key in ("ok", "status", "url", "http_status", "clean_text"):
        assert key in out, f"{scenario} 分支缺 key: {key}"


# ----------------------------------------------------------------------
# R5b  缓存命中不应被 Crawl-delay 阻塞
# ----------------------------------------------------------------------
def test_cache_hit_does_not_wait_on_crawl_delay():
    """命中缓存时根本不会访问站点, 不该为 Crawl-delay 排队。"""
    sc = _SpyCache(hit={"clean_text": "缓存正文", "segments": [],
                        "title": "缓存标题", "publish_time": None})
    limiter = _SpyLimiter()
    eng = make_engine(FetchOutcome(url="http://a.cn/x", status=200,
                                   text="<p>x</p>" * 200),
                      robots_delay=30.0, limiter=limiter)
    eng.cache = sc
    out = eng.fetch("http://a.cn/x", mode="http")
    assert out["cached"] is True
    assert limiter.calls == [], f"缓存命中仍被限速阻塞: {limiter.calls}"


def test_cache_miss_still_waits_on_crawl_delay():
    limiter = _SpyLimiter()
    eng = make_engine(FetchOutcome(url="http://a.cn/x", status=200,
                                   text="<p>x</p>" * 200),
                      robots_delay=30.0, limiter=limiter)
    eng.cache = _SpyCache()
    eng.fetch("http://a.cn/x", mode="http")
    assert limiter.calls == [30.0]


# ----------------------------------------------------------------------
# 组合: 分词修复后, 检索链路端到端不再放行字典页
# ----------------------------------------------------------------------
def test_irrelevant_provider_dropped_end_to_end():
    class JunkProvider(StaticProvider):
        name = "junk"

        def search(self, query, options, ctx):
            return [mk("啥的意思", "https://www.zdic.net/hans/%E5%92%80"),
                    mk("啥字怎么读", "https://dict.baidu.com/s?wd=%E5%92%80")]

    from intellisearch.retrieval.query import rewrite
    parsed = rewrite("为啥我的 fastapi 请求一并发就502", QueryOptions())
    results, reports = parallel_search([JunkProvider()], parsed,
                                       QueryOptions(top_k=5), None, None)
    assert results == [], "字典页不应作为 fastapi 502 的答案返回"
    assert reports[0].status == "irrelevant"