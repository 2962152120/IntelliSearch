"""多源并行检索与结果融合(去重 / 合并 / 排序)。

流程: 并行调源 -> 收集报告 -> 去重合并 -> 过滤 -> 排序 -> 截断
"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

from ..config import Config, DEFAULT_CONFIG
from ..errors import IntelliSearchError
from ..models import ParsedQuery, ProviderReport, QueryOptions, SearchResult
from ..providers.base import ProviderContext, SearchProvider
from ..providers.helpers import needs_resolve
from .rank import filter_results, limit_results, rank

log = logging.getLogger("intellisearch.fusion")

# 标题相似度阈值(超过即视为同一条结果)
TITLE_SIM_THRESHOLD = 0.88

# 检索源"整批结果与查询无关"的判定: 命中查询词才算相关。
#
# 为什么不用"命中任意一个词就算过"(旧实现):
#   `any(_hit_terms(...))` 只要 1 条结果命中 1 个词就放行**整批**, 其余
#   全部无关结果一起交给大模型当引用。实测把 14 条垃圾(伊朗局势时间线/
#   京东活动/百度一下...)配 1 条真结果, 整批 14 条全部放行 —— 等于闸门
#   形同虚设。而检索词里还混着切分残片("为啥"曾被切成"啥"), 连那唯一的
#   1 条命中都可能是"啥字字典页"。
#
# 现在要求"强证据 + 足够比例":
#   1. 判定只用实词 term(排除单字与疑问残片);
#   2. 命中的条数必须达到 min_hit_ratio(默认过半), 否则整批判为无关。
#      批次很小时(<=3)用"至少 1 条"兜底, 避免 1 条正常结果被误杀。
_WEAK_TERMS = {"什么", "怎么", "如何", "哪些", "哪个", "多少", "几个",
               "为何", "为啥", "是否", "可以", "请问", "一个", "一下",
               "为什么", "有没有", "是不是", "哪种", "哪里", "哪些",
               "能不能", "会不会", "要不要", "怎么办"}

# 权威域名: 只命中 1 个实词时, 这些站点的结果仍算可信
# (官网/官方文档确实常以产品名单页的形式命中)。
_AUTHORITATIVE = ("python.org", "docs.", "developer.", "github.com",
                  "microsoft.com", "mozilla.org", "w3.org", "runoob.com",
                  "fastapi.tiangolo.com", "nginx.org", "apache.org",
                  "oracle.com", "golang.org", "rust-lang.org", "npmjs.com")


def _is_authoritative(r: SearchResult) -> bool:
    url = (r.url or "").lower()
    return any(m in url for m in _AUTHORITATIVE)


def _strong_terms(terms: Sequence[str]) -> List[str]:
    """筛出可用于相关性判定的实词 term(排除单字与疑问残片)。"""
    out = []
    for t in terms:
        if not t:
            continue
        tl = t.strip().lower()
        if len(tl) < 2 or tl in _WEAK_TERMS:
            continue
        out.append(t)
    return out


def _hit_terms(r: SearchResult, terms: Sequence[str]) -> bool:
    hay = f"{r.title} {r.snippet} {r.url}".lower()
    return any(t and t.lower() in hay for t in terms)


def _hit_count(r: SearchResult, terms: Sequence[str]) -> int:
    """这条结果命中了**多少个**不同的实词(标题/摘要/URL 全文)。"""
    hay = f"{r.title} {r.snippet} {r.url}".lower()
    return sum(1 for t in terms if t and t.lower() in hay)


# 匹配位置的可信度: 标题/摘要命中是内容相关, 仅 URL 命中往往只是域名
# 恰好含查询词(实测 "AMD Zen5 架构" 返回 amd.com 官网/shop.amd.com, 只是
# 域名里有 "amd"), 这种命中不能算"相关"。
def _hits_content(r: SearchResult, terms: Sequence[str]) -> int:
    hay = f"{r.title} {r.snippet}".lower()
    return sum(1 for t in terms if t and t.lower() in hay)


def _drop_irrelevant(items: List[SearchResult],
                     terms: Sequence[str]) -> Tuple[List[SearchResult], str]:
    """过滤明显不相关的逐条结果, 返回 (保留项, 原因)。

    为什么需要: 搜索源被限流后不一定返回 403/验证码页, 而是返回
    **200 + 结构完整但内容是热门无关内容**的结果页(实测 Bing 返回过
    "伊朗局势时间线""京东十月份活动"来回答 "AMD Zen5 架构")。
    解析器能正常解析, 计数也是 10, 但对调用方而言是**有害的**: 这些
    链接会被当作检索证据直接写进答案。

    判定分两层:
    1. 逐条过滤: 一条结果要命中 >=2 个实词才保留; 只命中 1 个的通常是
       泛化首页/词条页(实测 "AMD Zen5 架构" 返回 amd.com 官网、shop.amd.com,
       只是域名里有 amd)。例外: 标题/摘要命中且域名权威(官方文档站)时保留。
    2. 整批判定: 全部被过滤掉才说明整个结果页被替换, 此时整批丢弃。
       只要还剩一条真结果, 就保留过滤后的子集 —— 避免把唯一的正确结果
       连同垃圾一起扔掉。
    """
    if not items or not terms:
        return items, ""
    strong = _strong_terms(terms)
    if not strong:
        # 检索词全是疑问词/虚词(实测 "什么是" -> []) 时没有可用证据:
        # 拿弱词硬匹配等于没闸门(几乎任何页面都含"什么"), 而据此判无关
        # 又会误杀真结果。此时选择不过滤。
        return items, ""
    multi = len(strong) >= 2
    kept = []
    for r in items:
        content = _hits_content(r, strong)
        total = _hit_count(r, strong)
        if content >= 2 or total >= 2:
            kept.append(r)          # 命中 >=2 个实词: 明确相关
        elif content >= 1 and not multi:
            kept.append(r)          # 单实词查询: 标题/摘要命中即可
        elif content >= 1 and _is_authoritative(r):
            kept.append(r)          # 命中 1 个但域名权威(官网/文档站)
    if kept:
        # 过滤后还剩至少一条真结果: 保留过滤后的子集。
        # 全部被过滤掉才说明整个结果页被替换(反爬软封), 此时整批丢弃 ——
        # 宁可返回空并说明原因, 也不能把无关链接当引用证据。
        return kept, ""
    return [], (f"结果与查询无关(0/{len(items)} 条相关, 查询词 "
                f"{'/'.join(strong[:4])}), 疑似反爬软封导致结果页被替换")


def parallel_search(providers: Sequence[SearchProvider], parsed: ParsedQuery,
                    options: QueryOptions, ctx: ProviderContext,
                    cfg: Config = None) -> Tuple[List[SearchResult], List[ProviderReport]]:
    """并行调用所有可用检索源。单源失败不影响其它源。"""
    cfg = cfg or DEFAULT_CONFIG
    usable = [p for p in providers if p.is_available()]
    if not usable:
        return [], [ProviderReport(name="*", ok=False, status="skipped",
                                   error="没有可用的检索源")]

    results: List[SearchResult] = []
    reports: List[ProviderReport] = []

    def run(p: SearchProvider) -> Tuple[SearchProvider, List[SearchResult],
                                        ProviderReport]:
        t0 = time.time()
        rep = ProviderReport(name=p.name, ok=False)
        try:
            items = p.search(parsed.effective, options, ctx) or []
            items, why = _drop_irrelevant(items, parsed.terms)
            if why:
                # 源返回了内容, 但没有一条与查询词有交集 —— 判定为"结果页被替换"
                # (反爬软封的典型表现: 200 + 结构完整的页面, 内容全是无关热门内容)。
                # 这类结果**必须丢掉**: 工具的产出是给大模型当引用来源的,
                # 宁可不返回, 也不能返回一批看起来正常的无关链接。
                rep.status = "irrelevant"
                rep.error = why
                log.warning("检索源 %s 返回结果与查询无关, 已丢弃: %s", p.name, why)
                return p, [], rep
            rep.ok = True
            rep.count = len(items)
            rep.status = "ok" if items else "empty"
            return p, items, rep
        except IntelliSearchError as e:
            rep.status = _status_of(e)
            rep.error = f"{e.code}: {e.message}"
        except Exception as e:                       # noqa: BLE001
            rep.status = "error"
            rep.error = f"{type(e).__name__}: {e}"
        finally:
            rep.elapsed_ms = int((time.time() - t0) * 1000)
        return p, [], rep

    workers = min(cfg.provider_max_concurrency, len(usable)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(run, p) for p in usable]
        for fut in as_completed(futs):
            p, items, rep = fut.result()
            reports.append(rep)
            results.extend(items)
            if not rep.ok:
                log.warning("检索源 %s 失败: %s", p.name, rep.error)

    # 结果严重不足(<2 条)时, 才用改写出的备选 query 补检索(只打最强的一个源)。
    # 阈值刻意设得很低: 补查会额外消耗下游配额, 只在主 query 明显失效时才值得。
    if len(results) < 2 and parsed.expanded:
        filler = usable[0]
        for q in parsed.expanded[:1]:
            if len(results) >= options.top_k * 2:
                break
            t0 = time.time()
            try:
                extra = filler.search(q, options, ctx) or []
                # 补查同样要过相关性闸门: 被软封的源换个 query 拿回来的还是
                # 无关热门内容, 放进来的话闸门就白设了。
                extra, why = _drop_irrelevant(extra, parsed.terms)
                for r in extra:
                    r.extra["_alt_query"] = q
                results.extend(extra)
                reports.append(ProviderReport(
                    name=f"{filler.name}#alt", ok=not why,
                    count=len(extra),
                    status=("irrelevant" if why else
                            ("ok" if extra else "empty")),
                    error=why,
                    elapsed_ms=int((time.time() - t0) * 1000)))
            except Exception as e:                   # noqa: BLE001
                log.warning("补充检索失败 [%s]: %s", q, e)

    reports.sort(key=lambda r: (not r.ok, r.name))
    return results, reports


def _status_of(e: IntelliSearchError) -> str:
    code = getattr(e, "code", "")
    return {
        "TIMEOUT": "timeout",
        "BLOCKED": "blocked",
        "UPSTREAM_ERROR": "error",
        "RATE_LIMITED": "rate_limited",
        "ROBOTS_DISALLOWED": "robots_blocked",
        "SAFETY_BLOCKED": "safety_blocked",
        "NO_RESULTS": "empty",
    }.get(code, "error")


# --------------------------------------------------------------------------
# 去重
# --------------------------------------------------------------------------
def _title_key(t: str) -> str:
    import re
    return re.sub(r"[\s\-_|,，。、:：·•\"'《》\[\]()（）]+", "", (t or "").lower())


def dedupe(results: Sequence[SearchResult]) -> List[SearchResult]:
    """URL 归一化去重 + 标题相似度去重, 并做字段合并补全。"""
    merged: Dict[str, SearchResult] = {}
    order: List[str] = []

    for r in results:
        if needs_resolve(r.url):
            continue          # 未能还原的跳转链接直接丢弃(无法溯源)
        r.extra.setdefault("sources", [r.source] if r.source else [])
        fp = r.fingerprint
        if fp in merged:
            _merge_into(merged[fp], r)
            continue
        # 标题相似度二次判定
        dup_key = None
        tk = _title_key(r.title)
        if tk:
            for key, exist in merged.items():
                ek = _title_key(exist.title)
                if not ek:
                    continue
                if tk == ek or (min(len(tk), len(ek)) >= 6 and
                                SequenceMatcher(None, tk, ek).ratio() >=
                                TITLE_SIM_THRESHOLD):
                    dup_key = key
                    break
        if dup_key:
            _merge_into(merged[dup_key], r)
        else:
            merged[fp] = r
            order.append(fp)

    for r in merged.values():
        r.extra["source_count"] = len(r.extra.get("sources", [r.source]) or [r.source])
    return [merged[k] for k in order]


def _merge_into(keep: SearchResult, new: SearchResult) -> None:
    """把 new 的信息合并进 keep: 补全缺失字段, 保留更优字段。"""
    srcs = keep.extra.setdefault("sources", [keep.source] if keep.source else [])
    if new.source and new.source not in srcs:
        srcs.append(new.source)
    # 标题: 取更"完整"的(更长且不含明显截断符)
    if _title_score(new.title) > _title_score(keep.title):
        keep.title = new.title
    # 摘要: 取更长的
    if len(new.snippet or "") > len(keep.snippet or ""):
        keep.snippet = new.snippet
    # 时间: 保留更"早"还是更"具体"? 取非空的第一个; 若都有, 取较新的
    if not keep.publish_time and new.publish_time:
        keep.publish_time = new.publish_time
    elif keep.publish_time and new.publish_time:
        try:
            kd, nd = keep.publish_dt, new.publish_dt
            if kd and nd and nd > kd:
                keep.publish_time = new.publish_time
        except Exception:
            pass
    if not keep.clean_text and new.clean_text:
        keep.clean_text = new.clean_text
    for k, v in (new.extra or {}).items():
        if k not in keep.extra:
            keep.extra[k] = v


def _title_score(t: str) -> float:
    if not t:
        return 0.0
    s = float(len(t))
    if t.rstrip().endswith(("...", "…")):
        s -= 8
    return s


# --------------------------------------------------------------------------
# 融合
# --------------------------------------------------------------------------
def fuse(results: Sequence[SearchResult], parsed: ParsedQuery,
         options: QueryOptions, cfg: Config = None,
         time_from=None, time_to=None) -> Tuple[List[SearchResult], Dict[str, int]]:
    """去重 -> 过滤 -> 排序 -> 截断。"""
    cfg = cfg or DEFAULT_CONFIG
    deduped = dedupe(results)
    kept, stats = filter_results(deduped, options, time_from, time_to)
    terms = parsed.terms or parsed.must or [parsed.effective]
    ranked = rank(kept, terms, parsed.effective, cfg, options)
    # 多源命中加成(共识度): 越多独立源命中, 越可信
    for r in ranked:
        cnt = r.extra.get("source_count", 1)
        bonus = 1.0 + 0.06 * max(0, cnt - 1)
        r.extra["_score"] = round(r.extra.get("_score", 0.0) * bonus, 4)
    ranked.sort(key=lambda x: x.extra.get("_score", 0.0), reverse=True)
    stats["deduped"] = len(results) - len(deduped)
    return limit_results(ranked, options.top_k), stats
