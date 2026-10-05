"""排序与过滤。

排序权重(需求二.4): 相关性 > 信息时效性 > 网站可信度 > 内容长度
同时过滤: 广告页 / 采集站镜像 / 站点黑白名单 / 时间范围外结果。
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from urllib.parse import urlparse

from ..config import Config, DEFAULT_CONFIG
from ..extract.clean import domain_of, is_spam, root_domain
from ..models import Freshness, QueryOptions, SearchResult, SourceType, now_utc

# 类型权重(可信度微调)
TYPE_CRED_ADJUST = {
    SourceType.DOC: 0.10,
    SourceType.WIKI: 0.05,
    SourceType.ACADEMIC: 0.10,
    SourceType.NEWS: 0.02,
    SourceType.PRODUCT: 0.03,
    SourceType.CODE: 0.06,
    SourceType.BLOG: -0.02,
    SourceType.FORUM: -0.06,
    SourceType.SOCIAL: -0.12,
    SourceType.PDF: 0.04,
    SourceType.UNKNOWN: 0.0,
}

# 明显低质域名(采集/镜像站)
LOW_QUALITY_DOMAINS = {
    "baijiahao.baidu.com": -0.15,
    "sohu.com": -0.03,
    "360doc.com": -0.2,
    "docin.com": -0.15,
    "doc88.com": -0.15,
    "wenku.baidu.com": -0.1,
}

FRESHNESS_HALF_LIFE_DAYS = 180.0


# --------------------------------------------------------------------------
# 单维度评分
# --------------------------------------------------------------------------
def score_relevance(r: SearchResult, terms: Sequence[str],
                    full_query: str = "") -> float:
    """相关性: 词项在标题/摘要的覆盖 + 精确匹配加成。"""
    if not terms:
        return 0.5
    title = (r.title or "").lower()
    snip = (r.snippet or "").lower()
    body = (r.clean_text or "").lower()[:2000]
    hit_t = hit_s = 0.0
    for t in terms:
        tl = str(t).lower()
        if len(tl) < 1:
            continue
        if tl in title:
            hit_t += 1.0
        elif tl in snip:
            hit_s += 0.55
        elif tl in body:
            hit_s += 0.35
    n = max(len(terms), 1)
    base = (hit_t / n) * 0.75 + (hit_s / n) * 0.25

    # 完整 query 出现在标题 -> 强信号
    if full_query:
        fq = full_query.lower().strip()
        if len(fq) > 4 and fq in title:
            base += 0.25
        elif len(fq) > 4 and fq in snip:
            base += 0.08
    # 型号类 token(含数字+字母)命中标题, 额外加权
    for t in terms:
        if re.search(r"[a-z]", str(t), re.I) and re.search(r"\d", str(t)) and \
                str(t).lower() in title:
            base += 0.08
    return max(0.0, min(1.0, base))


def score_freshness(r: SearchResult) -> float:
    """时效性: 指数衰减; 无发布时间给中性分(不惩罚, 因为多数网页本就没有)。"""
    dt = r.publish_dt
    if dt is None:
        return 0.45
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (now_utc() - dt).total_seconds() / 86400.0
    except Exception:
        return 0.45
    if days < 0:          # 未来时间(时区误差), 视为最新
        days = 0.0
    return max(0.05, math.exp(-days / FRESHNESS_HALF_LIFE_DAYS))


def credibility_of(r: SearchResult, cfg: Config = None) -> float:
    """可信度: 域名表 + 页面类型 + 低质域名惩罚。"""
    cfg = cfg or DEFAULT_CONFIG
    dom = domain_of(r.url)
    root = root_domain(dom)
    score = cfg.credibility_overrides.get(dom,
                                          cfg.credibility_overrides.get(root, 0.5))
    score += TYPE_CRED_ADJUST.get(r.source_type, 0.0)
    score += LOW_QUALITY_DOMAINS.get(dom, 0.0)
    return max(0.05, min(1.0, score))


def score_length(r: SearchResult, ideal: int = 260) -> float:
    """内容长度: 过短信息不足, 过长噪声多, 取适中。"""
    n = len(r.clean_text or r.snippet or "")
    if n == 0:
        return 0.2
    if n <= ideal:
        return 0.5 + 0.5 * (n / ideal)
    return max(0.35, 1.0 - min(1.0, (n - ideal) / (ideal * 6)))


# --------------------------------------------------------------------------
# 综合排序
# --------------------------------------------------------------------------
def rank(results: List[SearchResult], terms: Sequence[str],
         full_query: str = "", cfg: Config = None,
         options: QueryOptions = None) -> List[SearchResult]:
    """计算综合得分并原地排序。"""
    cfg = cfg or DEFAULT_CONFIG
    for r in results:
        if not r.source_type or r.source_type == SourceType.UNKNOWN:
            r.source_type = _detect(r)
        rel = score_relevance(r, terms, full_query)
        fresh = score_freshness(r)
        cred = credibility_of(r, cfg)
        leng = score_length(r)
        # 时效性权重在有明确时间需求时提高
        w_f = cfg.w_freshness
        if options and options.freshness != Freshness.ANY:
            w_f *= 2.0
        total = (cfg.w_relevance * rel + w_f * fresh +
                 cfg.w_credibility * cred + cfg.w_length * leng)
        r.relevance_score = round(rel, 4)
        r.credibility = round(cred, 4)
        r.extra["_score"] = round(total / (cfg.w_relevance + w_f +
                                           cfg.w_credibility + cfg.w_length), 4)
        r.extra["_fresh"] = round(fresh, 4)
    results.sort(key=lambda x: x.extra.get("_score", 0.0), reverse=True)
    return results


def _detect(r: SearchResult) -> SourceType:
    from ..extract.clean import detect_source_type
    return detect_source_type(r.url, r.title, r.snippet)


# --------------------------------------------------------------------------
# 过滤
# --------------------------------------------------------------------------
# 搜索引擎自身的中间页(AI 摘要页/跳转页), 不是真实来源, 必须剔除
_SERP_HOSTS = ("so.com", "sogou.com", "bing.com", "baidu.com", "google.com",
               "so.toutiao.com", "sm.cn", "quark.cn", "yandex.com", "duckduckgo.com")
_SERP_PATH_RE = re.compile(r"(?i)^/(search|link|s|web|url|jump|redirect|"
                           r"rogue|s\?|search\?)")


def _is_serp_intermediate(url: str) -> bool:
    """判断是否为搜索引擎自身的中间页。

    主站(so.com / baidu.com / bing.com 等)一律剔除;
    子产品(baike.baidu.com / zhidao.baidu.com 等)仅当其路径是搜索类时才剔除。
    """
    try:
        p = urlparse(url)
    except Exception:
        return True
    host = (p.hostname or "").lower()
    path = p.path or "/"
    for h in _SERP_HOSTS:
        if host == h or host == f"www.{h}":
            return True
        if host.endswith(f".{h}"):
            return bool(_SERP_PATH_RE.match(path))
    return False


def filter_results(results: Iterable[SearchResult], options: QueryOptions,
                   time_from: Optional[datetime] = None,
                   time_to: Optional[datetime] = None,
                   drop_spam: bool = True) -> Tuple[List[SearchResult], Dict[str, int]]:
    """按站点/时间/垃圾内容过滤。返回 (保留, 各阶段剔除计数)。"""
    stats = {"site": 0, "time": 0, "spam": 0, "empty": 0, "serp": 0}
    kept: List[SearchResult] = []
    for r in results:
        if not r.title or not r.url:
            stats["empty"] += 1
            continue
        if _is_serp_intermediate(r.url):
            stats["serp"] += 1
            continue
        dom = domain_of(r.url)
        root = root_domain(dom)
        if options.site:
            want = options.site.lower().lstrip(".")
            if want not in dom and want not in root:
                stats["site"] += 1
                continue
        if options.exclude_sites:
            blocked = [s.lower().lstrip(".") for s in options.exclude_sites]
            if any(b and (b in dom or b == root) for b in blocked):
                stats["site"] += 1
                continue
        if time_from or time_to:
            dt = r.publish_dt
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if time_from and dt < time_from:
                    stats["time"] += 1
                    continue
                if time_to and dt > time_to:
                    stats["time"] += 1
                    continue
        if drop_spam and is_spam(f"{r.title} {r.snippet}"):
            stats["spam"] += 1
            continue
        if drop_spam and _is_mirror(dom):
            stats["spam"] += 1
            continue
        kept.append(r)
    return kept, stats


_MIRROR_HINTS = ("mirror", "clone", "采集", "cnblogs.net", "w3cschool")


def _is_mirror(dom: str) -> bool:
    return any(h in dom for h in _MIRROR_HINTS)


def limit_results(results: List[SearchResult], top_k: int) -> List[SearchResult]:
    """数量上限控制(需求二.5)。"""
    return results[: max(1, top_k)]
