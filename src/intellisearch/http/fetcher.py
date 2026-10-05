"""渲染感知的抓取编排(Fetcher)。

三级降级链 —— 这是本模块的核心价值:

    Tier 1  轻量 HTTP          快(百毫秒级), 能覆盖约 70% 的公开页面
       │  失败 / 内容疑似不完整(JS 骨架页、空正文)
       ▼
    Tier 2  Headless Chromium  真实渲染, 能处理 SPA / JS 动态加载
       │  失败(内核不可用/超时/被拦)
       ▼
    Tier 3  降级返回            用已拿到的最好结果, 标注 degraded 原因

设计要点:
- 无需任何 API key: 全部使用本机浏览器内核 + 免费公开检索源;
- 内核缺失时不报错, 自动退回 Tier 1, 绝不因渲染不可用而整体失败;
- 渲染只在"确实需要"时启用, 避免为静态页付出 10 倍成本。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..config import Config, DEFAULT_CONFIG
from ..errors import IntelliSearchError
# RenderResult 在 http/__init__.py 里对外导出, 此处 import 是为了保持该路径可用
from .browser import (BrowserPool, RenderError, RenderRequest, RenderResult,
                      _is_timeout, get_pool)
from .client import HttpClient

log = logging.getLogger("intellisearch.fetcher")


@dataclass
class FetchOutcome:
    """一次抓取的最终结果 + 降级轨迹(可观测)。"""

    text: str
    url: str = ""
    status: int = 0
    fetch_mode: str = "http"            # http | render | none(兼容字段名)
    tier: str = "http"                  # 同 fetch_mode
    degraded: bool = False              # 是否走了降级
    upgrade_reason: str = ""            # 升级到渲染的原因(兼容字段名)
    reason: str = ""                    # 降级/失败原因
    attempts: List[str] = field(default_factory=list)   # 逐级尝试记录
    elapsed_ms: int = 0
    blocked_requests: int = 0
    rendered: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.text) and self.status and self.status < 400

    @property
    def status_code(self) -> int:
        """兼容别名。

        FetchResponse 用 status_code, 而检索源(providers/*)原本就是按
        status_code 读取的 —— 换成 FetchOutcome 后字段名变成 status,
        不提供这个别名会让所有源 AttributeError。
        """
        return self.status


class SmartFetcher:
    """统一抓取入口。持有一个 HTTP 客户端 + 一个浏览器池。

    命名保留 Smart 以兼容既有调用方。
    """

    # 某主机渲染确认无产出后, 多久内不再为它启动浏览器(秒)
    RENDER_DEAD_TTL = 600.0

    def __init__(self, config: Config = None, http: HttpClient = None,
                 pool: BrowserPool = None):
        self.cfg = config or DEFAULT_CONFIG
        self.http = http or HttpClient(self.cfg)
        self._pool = pool
        self.stats = {"http_ok": 0, "render_ok": 0, "degraded": 0, "failed": 0}
        self._dead_hosts: Dict[str, float] = {}

    # ---------- 浏览器池(惰性) ----------
    @property
    def pool(self) -> Optional[BrowserPool]:
        if self._pool is None and self.cfg.render_enabled:
            self._pool = get_pool(self.cfg, max_pages=self.cfg.render_max_pages,
                                  headless=self.cfg.render_headless)
        return self._pool

    def render_available(self) -> bool:
        p = self.pool
        return bool(p and p.available())

    def render_engine(self) -> str:
        p = self.pool
        return p.engine_name() if p else "none"

    # ---------- 主入口 ----------
    def fetch(self, url: str, *, mode: str = "auto", timeout: float = None,
              retries: int = None, lang: str = "zh", source: str = "",
              need_render: bool = None, wait_selector: str = "",
              settle_ms: int = 0, scroll: bool = False,
              params: Dict[str, Any] = None,
              headers: Dict[str, str] = None,
              rescue_only: bool = False) -> FetchOutcome:
        """按需抓取页面。

        mode:
            "auto"    轻量 HTTP 优先, 内容不完整时自动升级到渲染(默认)
            "http"    只用轻量 HTTP, 最快
            "render"  强制走浏览器渲染

        params/headers 会透传给底层 HTTP 客户端 —— 检索源靠 params 传查询串,
        缺了它会直接 TypeError(曾因漏传导致四个源全部失败)。

        rescue_only:
            True 时, 渲染只作为"救援"手段 —— 仅当 Tier 1 真的没拿到可用内容
            (异常 / 被拦截状态码 / 空正文 / 命中验证页特征)才启动浏览器;
            **不**因为"文本密度低"这类启发式判断而升级。

            这是检索源(providers)必须走的路径: 搜索结果页本身就是脚本多、
            可见文字少的页面, 启发式必然判定"不完整"; 而浏览器重渲染后的 DOM
            与原始 HTML 结构不同(Bing SERP 渲染后 `b_algo` 块数归零),
            结果是**把一个能正常解析的源活活渲染成 0 条**, 且每次多付数秒。
            正文抓取则相反 —— 那里文本密度低确实意味着 JS 骨架页, 用 "auto"。
        """
        t_all = time.time()
        timeout = timeout or self.cfg.timeout
        retries = self.cfg.retries if retries is None else retries
        if need_render is None:
            need_render = (self.cfg.render_enabled and mode != "http")
        force_render = (mode == "render")
        # 强制渲染: 即使 HTTP 拿到了内容, 也仍然走一遍浏览器以执行 JS
        must_render = force_render
        out = FetchOutcome(text="", url=url)
        out.fetch_mode = "none"
        out.tier = "none"

        # Tier 1: 轻量 HTTP(始终先试, 多数页面到这里就够了)
        try:
            resp = self.http.get(url, timeout=timeout, retries=retries,
                                 lang=lang, source=source or "fetch",
                                 params=params, headers=headers)
            out.attempts.append(f"http:{resp.status_code}")
            if resp.ok and resp.text:
                out.text = resp.text
                out.url = resp.url
                out.status = resp.status_code
                self.stats["http_ok"] += 1
                if need_render and not must_render:
                    if not self._should_upgrade(resp, rescue_only):
                        marker = self._block_marker(resp.text)
                        if marker:
                            # 被反爬了, 但渲染救不回来(SERP 重渲染后 DOM 结构
                            # 会变, 选择器失效)—— 只留诊断痕迹, 不白跑一趟。
                            log.info("疑似验证页(%s), 不升级渲染: %s", marker, url)
                        out.fetch_mode = out.tier = "http"
                        out.elapsed_ms = int((time.time() - t_all) * 1000)
                        return out
                    out.upgrade_reason = ("HTTP 被拦截, 尝试浏览器渲染"
                                          if rescue_only
                                          else "http 内容疑似 JS 骨架页")
                    log.debug("升级渲染: %s", url)
                elif must_render:
                    out.upgrade_reason = "强制渲染模式"
        except IntelliSearchError as e:
            out.attempts.append(f"http_err:{e.code}")
            out.reason = f"HTTP 抓取失败({e.code})"
            out.upgrade_reason = "HTTP 抓取失败, 尝试浏览器渲染"
            log.debug("HTTP 抓取失败 %s: %s", url, e)
        except Exception as e:      # noqa: BLE001
            out.attempts.append(f"http_err:{type(e).__name__}")
            out.reason = f"HTTP 抓取异常({type(e).__name__})"
            out.upgrade_reason = "HTTP 抓取异常, 尝试浏览器渲染"

        # degraded 的契约(与 test_render.py 一致): 表示"最终 tier 低于预期",
        # 即没能按理想路径拿到内容。它描述的是**过程**, 不代表内容不可用 ——
        # 内容是否可用看 ok/text。两者不能混为一谈, 否则调用方无法区分。
        if not need_render:
            # 明确只要 HTTP(mode=http 或渲染被禁用): 这就是预期路径,
            # 成功拿到完整内容不算降级。
            out.fetch_mode = out.tier = ("http" if out.text else "none")
            out.degraded = (not out.text) or (
                bool(out.text) and self._looks_incomplete(out.text, url,
                                                          out.status))
            if out.degraded:
                out.reason = out.reason or "内容疑似不完整, 但已按 http 模式返回"
                self.stats["degraded"] += 1
            if not out.text:
                self.stats["failed"] += 1
            out.elapsed_ms = int((time.time() - t_all) * 1000)
            return out

        # Tier 2: Headless Chromium
        pool = self.pool
        # 用户显式要求渲染时不能被"无效主机"记忆挡住 —— 那是内部省时间的
        # 启发式, 不是对外契约。否则 mode="render" 会静默拿回未渲染的原文。
        if not must_render and self._render_dead(url):
            # 该主机最近一次渲染已被证明拿不到东西(4xx/5xx 或空内容), 短期内
            # 不再为它重复启动浏览器 —— 每次启动要 5~10 秒, 而同一个被封的
            # 源在每轮检索里都会被访问一次。
            out.attempts.append("render:skip_recent_failure")
            out.fetch_mode = out.tier = ("http" if out.text else "none")
            out.degraded = True
            out.reason = out.reason or "近期该主机渲染无产出, 跳过重复渲染"
            self.stats["degraded"] += 1
            if not out.text:
                self.stats["failed"] += 1
            out.elapsed_ms = int((time.time() - t_all) * 1000)
            return out
        if pool is None or not pool.available():
            out.attempts.append("render:unavailable")
            out.fetch_mode = out.tier = ("http" if out.text else "none")
            # 渲染内核不可用本身就是一次降级(没走理想路径)
            out.degraded = True
            out.reason = out.reason or "渲染内核不可用, 已降级为轻量抓取"
            self.stats["degraded"] += 1
            if not out.text:
                self.stats["failed"] += 1
            out.elapsed_ms = int((time.time() - t_all) * 1000)
            return out

        req = RenderRequest(
            url=url, timeout=self.cfg.render_timeout,
            wait_until=self.cfg.render_wait_until,
            wait_selector=wait_selector or self.cfg.render_wait_selector,
            settle_ms=settle_ms or self.cfg.render_settle_ms,
            scroll=scroll or self.cfg.render_scroll,
            lang=lang,
            block_resources=self.cfg.render_block_resources,
        )
        try:
            r = pool.render(req)
            out.attempts.append(f"render:{r.status}")
            if r.html:
                out.text = r.html
                out.url = r.final_url or url
                out.status = r.status or 200
                out.fetch_mode = out.tier = "render"
                out.rendered = True
                out.blocked_requests = r.blocked_requests
                self.stats["render_ok"] += 1
                # 渲染成功 = Tier 1 的失败已被救回, 原因要清掉, 否则会出现
                # "tier=render 且成功" 却挂着 "HTTP 抓取失败(BLOCKED)" 的自相矛盾。
                out.reason = ""
                self._note_render(url, r)
                out.elapsed_ms = int((time.time() - t_all) * 1000)
                return out
            out.reason = "渲染返回空内容"
            self._note_render(url, r)
        except RenderError as e:
            out.attempts.append(f"render_err:{e.code}")
            out.reason = f"渲染失败({e.code})"
            # 渲染也救不回来的硬失败同样记一笔, 免得同一个被封的源每轮检索
            # 都重新起一次浏览器(5~10 秒)。超时不记 —— 那是瞬时抖动, 记进去
            # 会让该主机十分钟内再也不渲染。
            if not _is_timeout(e):
                self._note_render(url, None)
            log.debug("渲染失败 %s: %s", url, e)
        except Exception as e:          # noqa: BLE001
            out.attempts.append(f"render_err:{type(e).__name__}")
            out.reason = f"渲染异常({type(e).__name__})"

        # Tier 3: 降级返回已拿到的最好结果
        # degraded=True 表示没能走完理想路径(契约见 test_render.py);
        # 彻底没抓到内容时另外计 failed, 便于区分"内容少"和"完全失败"。
        out.degraded = True
        self.stats["degraded"] += 1
        if not out.text:
            self.stats["failed"] += 1
        out.fetch_mode = out.tier = ("render" if out.rendered else
                                     ("http" if out.text else "none"))
        out.elapsed_ms = int((time.time() - t_all) * 1000)
        return out

    def info(self) -> Dict:
        """运行时信息, 供 /stats 端点与排障。"""
        return {
            "render_enabled": self.cfg.render_enabled,
            "render_available": self.render_available(),
            "engine": self.render_engine(),
            # 实际内核的绝对路径: 确认"用的是包内内置内核还是系统浏览器"
            "engine_detail": self._render_detail(),
            "headless": self.cfg.render_headless,
            "max_pages": self.cfg.render_max_pages,
            "block_resources": self.cfg.render_block_resources,
            "stats": dict(self.stats),
        }

    def _render_detail(self) -> str:
        """内核绝对路径(排障用)。渲染不可用时返回空串, 不抛异常。"""
        p = self.pool
        if not p:
            return ""
        try:
            return p.engine_detail
        except Exception:                       # noqa: BLE001
            return ""

    # ---------- 渲染"无效主机"记忆 ----------
    def _note_render(self, url: str, r) -> None:
        """记录一次渲染有没有产出, 供后续跳过注定失败的重复渲染。

        判据只认**硬失败**: 4xx/5xx 或空内容。刻意不用"页面小"当判据 ——
        正常的小页面(短正文、接口响应)都要小于任何合理阈值, 按大小记会
        让同主机接下来十分钟里真正需要渲染的页面被一锅端地跳过。
        """
        status = getattr(r, "status", 0) or 0
        dead = r is None or status >= 400 or not (getattr(r, "html", "") or "").strip()
        if dead:
            # 顺手回收过期项, 避免长驻进程里字典无界增长
            now = time.time()
            for host, exp in list(self._dead_hosts.items()):
                if exp <= now:
                    self._dead_hosts.pop(host, None)
            self._dead_hosts[self._host(url)] = now + self.RENDER_DEAD_TTL

    def _render_dead(self, url: str) -> bool:
        return self._dead_hosts.get(self._host(url), 0.0) > time.time()

    @staticmethod
    def _host(url: str) -> str:
        try:
            return urlparse(url).netloc.lower()
        except Exception:                # noqa: BLE001
            return url

    # ---------- 内容完整性判断 ----------
    def _should_upgrade(self, resp, rescue_only: bool) -> bool:
        """Tier 1 拿到响应后, 是否值得再花几秒启动浏览器。"""
        if rescue_only:
            return self._needs_rescue(resp)
        return self._looks_incomplete(resp.text, resp.url, resp.status_code)

    @staticmethod
    def _needs_rescue(resp) -> bool:
        """只在 Tier 1 确实没拿到可用内容时才返回 True。

        与 `_looks_incomplete` 的区别: 后者是"内容可能不完整"的启发式,
        对搜索结果页必然为真; 这里只认硬信号 —— 状态码被拦、空正文。

        **刻意不认"小体积验证页"**: 200 + 几 KB 占位页确实是被反爬了, 但对
        检索源来说渲染并不能救回来 —— 浏览器重渲染后的 SERP 是另一棵树
        (实测: 360 渲染后 326 KB 的真实页面里 `res-list` 为 0, Bing 渲染后
        `b_algo` 从 10 变 0), 选择器全部失效, 结果仍是 0 条, 却要付 5~11 秒。
        所以这类页面只做诊断标记(见 `_block_marker`), 不再白跑一趟。
        """
        status = getattr(resp, "status_code", 0) or 0
        if status in (401, 403, 405, 429) or status >= 500:
            return True
        text = getattr(resp, "text", "") or ""
        return not text.strip()

    @staticmethod
    def _block_marker(text: str) -> str:
        """返回命中的验证页特征词(没有则为空串), 仅用于诊断与日志。

        检索源判定必须苛刻: 正常结果页也可能出现"验证码"等字样(比如搜索
        "验证码绕过"时正文里到处都是), 所以只在页面**同时**很小
        (真正的拦截页几乎都是几 KB 占位页, 正常 SERP 通常上百 KB)时才认。
        """
        if not text or len(text) > 30000:
            return ""
        low = text[:20000].lower()
        for token in ("captcha", "antispider", "安全验证", "人机验证",
                      "验证码", "访问异常", "请开启javascript",
                      "enable javascript"):
            if token in low:
                return token
        return ""

    @staticmethod
    def _looks_incomplete(html: str, url: str = "",
                          status_code: int = 200) -> bool:
        """判断 HTTP 直取的内容是否需要渲染才能拿到正文。

        委托给 renderer.needs_render —— 那是唯一的判定实现, 避免两处
        阈值各自漂移(曾出现 fetcher 与 renderer 各写一套、判据不一致)。
        保留本方法是为了兼容既有调用方与测试。
        """
        from .renderer import needs_render
        return needs_render(html, status_code)

    def close(self):
        self.http.close()
        if self._pool is not None:
            self._pool.close()


# 兼容别名
Fetcher = SmartFetcher
