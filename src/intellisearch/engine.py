"""统一检索入口 —— SearchEngine。

调用链::

    search(query)
      -> 参数校验
      -> 限流(全局/会话)
      -> 安全检查(黑名单)
      -> 缓存查询
      -> Query 解析与改写(含会话上下文)
      -> 多源并行检索(单源失败自动降级)
      -> 融合: 去重 / 过滤 / 排序 / 截断
      -> [可选] 正文抓取(遵守 robots, 带缓存)
      -> 摘要预提取 / 冲突检测 / 会话记忆
      -> 审计日志
      -> SearchResponse(永远返回对象, 不抛业务异常)

设计约束: 任何一步失败都必须收敛为明确的状态, 绝不让调用方拿到半个结果。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from .ai.conflict import detect_conflicts
from .ai.context import SessionStore, apply_context
from .ai.formatter import build_context, compress, summarize_results, to_markdown
from .cache import Cache
from .config import Config, DEFAULT_CONFIG
from .errors import (EmptyResultError, IntelliSearchError, RateLimitError,
                     RobotsDisallowed, SafetyBlocked)
from .extract.extractor import extract as extract_page
from .http.client import HttpClient
from .http.fetcher import SmartFetcher
from .http.robots import RobotsCache
from .models import (Freshness, ParsedQuery, ProviderReport, QueryOptions,
                     SearchResponse, SearchResult, Status)
from .providers import REGISTRY, DEFAULT_PROVIDERS
from .providers.base import ProviderContext, SearchProvider
from .retrieval.fusion import fuse, parallel_search
from .retrieval.query import merge_time_filter, rewrite, core_terms
from .safety.blacklist import SafetyGuard
from .safety.ratelimit import RateLimiter

log = logging.getLogger("intellisearch")


class SearchEngine:
    """对外唯一入口。线程安全, 建议全局复用一个实例(复用连接池)。"""

    def __init__(self, config: Config = None, providers: Sequence[str] = None,
                 http: HttpClient = None, cache: Cache = None,
                 ua_profile: str = "pc", rewriter: Callable[[str], List[str]] = None):
        self.cfg = config or DEFAULT_CONFIG
        self.http = http or HttpClient(self.cfg, ua_profile=ua_profile)
        self.fetcher = SmartFetcher(self.cfg, self.http)
        self.rewriter = rewriter

        names = list(providers or self.cfg.providers or DEFAULT_PROVIDERS)
        self.providers: List[SearchProvider] = []
        for n in names:
            cls = REGISTRY.get(n)
            if cls:
                self.providers.append(cls(self.cfg))
            else:
                log.warning("未知的检索源: %s(已忽略)", n)
        if not self.providers:
            self.providers = [REGISTRY[n](self.cfg) for n in DEFAULT_PROVIDERS]

        self.cache = cache if cache is not None else Cache(
            path=self.cfg.cache_path, ttl=self.cfg.cache_ttl,
            max_entries=self.cfg.cache_max_entries,
            enabled=self.cfg.cache_enabled)
        self.limiter = RateLimiter(
            global_qps=self.cfg.global_qps, global_burst=self.cfg.global_burst,
            session_qps=self.cfg.session_qps, session_burst=self.cfg.session_burst,
            enabled=self.cfg.rate_limit_enabled)
        self.guard = SafetyGuard(blacklist=self.cfg.blacklist_terms,
                                 block_suspicious=self.cfg.block_suspicious)
        self.sessions = SessionStore()
        self._robots = RobotsCache(
            fetcher=lambda u: self.http.get(u, timeout=5, retries=0),
            user_agent="*")
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def search(self, query: str, options: QueryOptions = None,
               **overrides) -> SearchResponse:
        """统一检索入口。

        :param query: 关键词或自然语言问题
        :param options: 检索参数; 也可直接以关键字参数传入(如 top_k=5)
        :return: SearchResponse —— 永不抛业务异常, 通过 status 判定结果
        """
        t0 = time.time()
        opts = self._merge_options(options, overrides)
        resp = SearchResponse(query=(query or "").strip(), status=Status.ERROR)

        # 1. 参数校验
        if not resp.query:
            resp.status = Status.ERROR
            resp.message = "查询词为空"
            return self._finish(resp, t0)
        if len(resp.query) > 500:
            resp.query = resp.query[:500]

        try:
            # 2. 限流
            self.limiter.acquire(opts.session_id)

            # 3. 安全检查
            self.guard.check_query(resp.query)

            # 4. 会话上下文关联
            if opts.session_id:
                resp.query = apply_context(resp.query, opts.session_id, self.sessions)

            # 5. Query 解析与改写
            parsed = rewrite(resp.query, opts, rewriter=self.rewriter)
            resp.parsed_query = parsed
            effective_query = parsed.effective
            time_from, time_to = merge_time_filter(opts, parsed)

            # 6. 缓存
            cache_key = opts.cache_key(effective_query)
            if opts.use_cache and self.cache.enabled:
                hit = self.cache.get(cache_key, "search")
                if hit:
                    cached = self._from_cache(hit, resp.query)
                    if cached:
                        cached.cached = True
                        # 缓存命中同样要更新会话记忆, 否则开启缓存后
                        # 后续轮次的指代消解会失效
                        self._remember(opts, cached.query, cached.parsed_query,
                                       cached.results)
                        self._audit(resp.query, "cache_hit", len(cached.results))
                        return self._finish(cached, t0)

            # 7. 多源并行检索
            ctx = ProviderContext(http=self.http, config=self.cfg,
                                  robots=self._robots, lang=opts.lang,
                                  fetcher=self.fetcher)
            raw, reports = parallel_search(self.providers, parsed, opts, ctx, self.cfg)
            resp.providers = reports
            resp.total_found = len(raw)

            # 8. 安全过滤
            raw, dropped = self.guard.filter_results(raw)
            if dropped:
                log.info("安全过滤剔除 %d 条结果", dropped)

            # 9. 融合: 去重 / 过滤 / 排序 / 截断
            results, stats = fuse(raw, parsed, opts, self.cfg, time_from, time_to)
            resp.results = results

            # 10. 正文抓取(可选)
            if opts.fetch_content and results:
                self._fetch_contents(results, opts, ctx)

            # 11. AI 适配: 摘要 + 冲突检测
            if opts.summarize:
                summarize_results(results, max_chars=min(300, opts.max_content_chars))
            terms = (parsed.terms or []) + core_terms(parsed.effective)[:4]
            if len(results) >= 2:
                try:
                    resp.conflicts = detect_conflicts(results, terms)
                except Exception as e:      # noqa: BLE001
                    log.debug("冲突检测失败: %s", e)

            # 12. 状态判定
            resp.status = self._decide_status(reports, results)
            resp.message = self._describe(resp, reports, stats)

            # 13. 写缓存(只缓存成功结果)
            if opts.use_cache and self.cache.enabled and resp.status in (
                    Status.OK, Status.PARTIAL):
                self.cache.set(cache_key, self._to_cache(resp), "search",
                               opts.cache_ttl)

            # 14. 会话记忆
            self._remember(opts, resp.query, parsed, resp.results)

            self._audit(resp.query, resp.status.value, len(results))
        except RateLimitError as e:
            resp.status = Status.ERROR
            resp.message = f"请求过于频繁: {e.message}"
            log.warning("限流: %s", e.message)
        except SafetyBlocked as e:
            resp.status = Status.BLOCKED
            resp.message = e.message
        except IntelliSearchError as e:
            resp.status = Status.ERROR
            resp.message = f"{e.code}: {e.message}"
            log.error("检索失败: %s", e)
        except Exception as e:                      # noqa: BLE001
            resp.status = Status.ERROR
            resp.message = f"未知错误: {type(e).__name__}: {e}"
            log.exception("检索异常")

        return self._finish(resp, t0)

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------
    def search_json(self, query: str, options: QueryOptions = None,
                    max_chars: int = None, **overrides) -> str:
        """检索并返回 JSON 字符串(可直接喂给大模型)。"""
        resp = self.search(query, options, **overrides)
        payload = resp.to_dict()
        if max_chars:
            payload = compress(payload, max_chars)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def search_context(self, query: str, options: QueryOptions = None,
                       max_chars: int = 6000, **overrides) -> str:
        """检索并返回带引用编号的上下文文本。"""
        resp = self.search(query, options, **overrides)
        if not resp.results:
            return f"# 检索结果\n未找到相关内容（状态: {resp.status.value}）。{resp.message}"
        text = build_context(resp.results, max_chars=max_chars,
                             with_content=bool(options and options.fetch_content))
        if resp.conflicts:
            text += "\n\n⚠️ 信息差异:\n" + "\n".join(
                f"- {c['message']}" for c in resp.conflicts)
        return text

    def search_markdown(self, query: str, options: QueryOptions = None,
                        **overrides) -> str:
        resp = self.search(query, options, **overrides)
        return to_markdown(resp)

    def fetch(self, url: str, max_chars: int = 4000,
              respect_robots: bool = None,
              mode: str = "auto") -> Dict[str, Any]:
        """单独抓取并抽取某个页面的正文。

        mode:
            "auto"   轻量 HTTP 优先, 内容疑似 JS 骨架页时自动升级到浏览器渲染(默认)
            "http"   只用轻量 HTTP(最快, 适合确定是静态页的场景)
            "render" 强制走 Chromium 渲染(JS 动态页面)
        """
        respect = self.cfg.respect_robots if respect_robots is None else respect_robots
        delay = 0.0
        if respect:
            allowed, delay = self._robots.check(url)
            if not allowed:
                return {"url": url, "ok": False, "status": "robots_disallowed",
                        "http_status": 0, "clean_text": "", "message":
                        "robots.txt 禁止访问该地址"}
        ck = hashlib.md5(f"page:{url}:{max_chars}:{mode}".encode()).hexdigest()
        if self.cache.enabled:
            hit = self.cache.get(ck, "page")
            if hit:
                hit["cached"] = True
                return hit
        # 遵守站点声明的 Crawl-delay, 与 _fetch_contents 保持一致。
        # 放在缓存查询**之后**: 命中缓存时根本不会访问该站点, 不该为它排队。
        if delay:
            self.limiter.wait_host(url, crawl_delay=delay)
        try:
            r = self.fetcher.fetch(url, mode=mode,
                                    timeout=self.cfg.timeout, source="fetch")
            page = extract_page(r.text, url=url, max_chars=max_chars,
                                final_url=r.url)
            out = page.to_dict()
            # ok 必须反映"是否真的拿到内容"。
            # 不能只看 bool(r.text): 404 / 反爬验证页 / 浏览器错误页都有正文,
            # 会被判成成功并当作引用来源返回 —— 这是"假成功"。
            # FetchOutcome.ok 已含 status<400 判定, 这里直接采用, 并把
            # 真实 status 与失败原因透传给调用方。
            got = bool(r.ok)
            if got:
                status = "ok"
            elif r.status >= 400 or (r.status and not r.text):
                status = f"http_{r.status}" if r.status >= 400 else "no_content"
            else:
                status = r.reason or "no_content"
            out.update({"ok": got, "status": status,
                        "http_status": r.status,
                        "cached": False,
                        "fetch_mode": r.fetch_mode,
                        "rendered": r.rendered,
                        "degraded": r.degraded,
                        "reason": r.reason,
                        "upgrade_reason": r.upgrade_reason,
                        "attempts": r.attempts,
                        "engine": self.fetcher.render_engine()})
            if self.cache.enabled and got:
                # 只缓存成功结果。渲染超时 / 浏览器内核不可用属于**瞬时**
                # 故障, 缓存下来(且 ttl 是普通值的 4 倍)会让站点恢复后
                # 仍持续返回失败。
                self.cache.set(ck, out, "page", ttl=self.cfg.cache_ttl * 4)
            return out
        except IntelliSearchError as e:
            return {"url": url, "ok": False, "status": e.code,
                    "http_status": 0, "clean_text": "",
                    "message": e.message}
        except Exception as e:                      # noqa: BLE001
            return {"url": url, "ok": False, "status": "error",
                    "http_status": 0, "clean_text": "",
                    "message": f"{type(e).__name__}: {e}"}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _merge_options(self, options: QueryOptions,
                       overrides: Dict[str, Any]) -> QueryOptions:
        if options is None:
            return QueryOptions(**overrides) if overrides else QueryOptions()
        if not overrides:
            return options
        import dataclasses
        valid = {f.name for f in dataclasses.fields(QueryOptions)}
        for k, v in overrides.items():
            if k in valid:
                setattr(options, k, v)
            else:
                log.warning("忽略未知参数: %s", k)
        return options

    def _fetch_contents(self, results: List[SearchResult], opts: QueryOptions,
                        ctx: ProviderContext) -> None:
        """并发抓取正文, 遵守 robots, 单页失败不影响其它页。"""
        targets = results[: max(1, min(opts.top_k, 8))]

        def work(r: SearchResult):
            try:
                delay = 0.0
                if self.cfg.respect_robots:
                    allowed, delay = self._robots.check(r.url)
                    if not allowed:
                        r.extra["fetch_status"] = "robots_disallowed"
                        return
                ck = hashlib.md5(f"page:{r.url}:{opts.max_content_chars}"
                                 .encode()).hexdigest()
                if self.cache.enabled:
                    hit = self.cache.get(ck, "page")
                    if hit:
                        r.clean_text = hit.get("clean_text", "")
                        r.segments = hit.get("segments", [])
                        if hit.get("publish_time") and not r.publish_time:
                            r.publish_time = hit["publish_time"]
                        if hit.get("title") and len(hit["title"]) > len(r.title):
                            r.title = hit["title"]
                        r.extra["fetch_status"] = "cached"
                        r.fetched_at = datetime.now(timezone.utc).isoformat()
                        return
                # 缓存未命中才需要真正访问站点, 这时才按 Crawl-delay 排队。
                if delay:
                    self.limiter.wait_host(r.url, crawl_delay=delay)
                resp = self.fetcher.fetch(
                    r.url, mode="auto", timeout=opts.timeout, source="fetch")
                # 与 engine.fetch() 保持一致: 必须校验 ok, 不能只看有没有正文。
                # 404/403/反爬验证页都有正文, 直接抽成clean_text 会让
                # "页面不存在"这类内容作为引用来源喂给大模型。
                if not resp.ok:
                    r.extra["fetch_status"] = (
                        f"http_{resp.status}" if resp.status >= 400
                        else (resp.reason or "no_content"))
                    return
                page = extract_page(resp.text, url=r.url,
                                    max_chars=opts.max_content_chars,
                                    final_url=resp.url)
                r.clean_text = page.clean_text
                r.segments = page.segments
                if page.publish_time and not r.publish_time:
                    r.publish_time = page.publish_time
                if page.title and len(page.title) > len(r.title) * 1.2:
                    r.title = page.title
                if page.source_type:
                    r.source_type = page.source_type
                r.extra["fetch_status"] = resp.fetch_mode
                r.extra["http_status"] = resp.status
                r.extra["rendered"] = resp.rendered
                if resp.upgrade_reason:
                    r.extra["upgrade_reason"] = resp.upgrade_reason
                r.fetched_at = datetime.now(timezone.utc).isoformat()
                # 只缓存成功结果: 渲染超时/内核不可用是**瞬时**故障,
                # 缓存下来会让站点恢复后仍持续返回失败(ttl*4 很长)。
                if self.cache.enabled:
                    self.cache.set(ck, page.to_dict(), "page",
                                   self.cfg.cache_ttl * 4)
            except IntelliSearchError as e:
                r.extra["fetch_status"] = e.code
            except Exception as e:                  # noqa: BLE001
                r.extra["fetch_status"] = f"error:{type(e).__name__}"

        with ThreadPoolExecutor(max_workers=min(6, len(targets)) or 1) as pool:
            list(pool.map(work, targets))

    @staticmethod
    def _decide_status(reports: List[ProviderReport],
                       results: List[SearchResult]) -> Status:
        if results:
            ok = [r for r in reports if r.ok]
            failed = [r for r in reports if not r.ok]
            return Status.PARTIAL if failed else Status.OK
        if reports and all(not r.ok for r in reports):
            return Status.ERROR
        return Status.NO_RESULTS

    @staticmethod
    def _describe(resp: SearchResponse, reports: List[ProviderReport],
                  stats: Dict[str, int]) -> str:
        if resp.status == Status.OK:
            return (f"检索成功：{len(resp.results)} 条结果，"
                    f"去重 {stats.get('deduped', 0)} 条")
        if resp.status == Status.PARTIAL:
            bad = ", ".join(f"{r.name}({r.status})" for r in reports if not r.ok)
            return f"部分检索源失败[{bad}]，仍返回 {len(resp.results)} 条可用结果"
        if resp.status == Status.NO_RESULTS:
            # 有的源"响应正常但结果为空", 有的源被判定为不可用(整批结果与查询
            # 无关 = 反爬软封)。这两种情况给的建议完全不同, 不能一句带过。
            rejected = [r for r in reports if not r.ok]
            if rejected:
                bad = ", ".join(f"{r.name}({r.status})" for r in rejected)
                return (f"未找到匹配结果；{bad} 的返回被判为不可用"
                        f"（原因见 providers[].error，通常是目标站点反爬）。"
                        f"建议：稍后重试 / 换用其他检索源 / 配置代理")
            return ("检索源均正常响应，但未找到匹配结果。"
                    "建议：缩短查询词 / 去掉限定条件 / 放宽时间范围")
        bad = "; ".join(f"{r.name}: {r.status} {r.error}" for r in reports if not r.ok)
        return f"全部检索源失败：{bad or '未知原因'}"

    @staticmethod
    def _to_cache(resp: SearchResponse) -> Dict[str, Any]:
        return {
            "results": [r.to_dict() for r in resp.results],
            "parsed_query": resp.parsed_query.to_dict() if resp.parsed_query else None,
            "providers": [p.to_dict() for p in resp.providers],
            "conflicts": resp.conflicts,
            "total_found": resp.total_found,
            "status": resp.status.value,
            "message": resp.message,
        }

    @staticmethod
    def _from_cache(data: Dict[str, Any], query: str) -> Optional[SearchResponse]:
        try:
            results = []
            for d in data.get("results", []):
                d = dict(d)
                src = d.pop("source_type", "unknown")
                r = SearchResult(**{k: v for k, v in d.items()
                                    if k in SearchResult.__dataclass_fields__})
                from .models import SourceType
                try:
                    r.source_type = SourceType(src)
                except ValueError:
                    r.source_type = SourceType.UNKNOWN
                results.append(r)
            pq = data.get("parsed_query")
            return SearchResponse(
                query=query, status=Status(data.get("status", "ok")),
                results=results,
                parsed_query=ParsedQuery(**pq) if pq else None,
                providers=[ProviderReport(**p) for p in data.get("providers", [])],
                conflicts=data.get("conflicts", []),
                total_found=data.get("total_found", len(results)),
                message=data.get("message", ""),
            )
        except Exception as e:                      # noqa: BLE001
            log.warning("缓存反序列化失败, 忽略: %s", e)
            return None

    def _remember(self, opts: QueryOptions, query: str,
                  parsed: Optional[ParsedQuery], results=None) -> None:
        """记录会话实体, 供后续轮次的指代消解使用。"""
        if not opts.session_id or parsed is None:
            return
        try:
            self.sessions.remember(
                opts.session_id, query,
                entities=core_terms(parsed.effective)[:6],
                urls=[r.url for r in (results or [])[:5]])
        except Exception as e:                      # noqa: BLE001
            log.debug("会话记忆写入失败: %s", e)

    @staticmethod
    def _finish(resp: SearchResponse, t0: float) -> SearchResponse:
        resp.elapsed_ms = int((time.time() - t0) * 1000)
        return resp

    def _audit(self, query: str, status: str, n: int) -> None:
        if not self.cfg.audit_log:
            return
        try:
            d = os.path.dirname(os.path.abspath(self.cfg.audit_path))
            os.makedirs(d, exist_ok=True)
            line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                               "query": query, "status": status, "count": n},
                              ensure_ascii=False)
            with self._lock, open(self.cfg.audit_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:                           # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """运行时状态, 便于排障与监控。"""
        return {
            "providers": [{"name": p.name, "available": p.is_available()}
                          for p in self.providers],
            "cache": self.cache.info(),
            "rate_limit": self.limiter.stats(),
            "sessions": len(self.sessions),
            "fetcher": self.fetcher.info(),
        }

    def close(self):
        try:
            self.fetcher.close()
        finally:
            self.http.close()
            self.cache.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
