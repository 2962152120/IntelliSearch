"""Chromium 浏览器渲染器(基于 Playwright)。

为什么需要它: 很多站点对轻量 HTTP 直接返回 403 / 空壳 HTML(SPA),
实测知乎搜索页轻量 HTTP 仅 584 字节且状态码 403, 而浏览器渲染可拿到 43KB 完整 DOM。

核心能力:
- 可执行文件自动发现: 环境变量 → 系统 Chrome/Edge/Chromium/Brave
  → ms-playwright 缓存(任意版本) → playwright 自带, 全部不依赖第三方 API Key
- **单工作线程独占 Playwright**: 同步 API 绑定创建线程, 池化 browser 跨线程会随机失败
- 反检测: 抹掉 webdriver 指纹, 伪装完整浏览器环境
- 资源过滤: 默认拦截图片/视频/字体, 只保留文本渲染所需资源
- 等待策略: 支持 wait_until / 额外延时 / 等待特定元素出现
- 优雅降级: Playwright 未安装或内核不可用时, available() 返回 False,
  上层自动回退到纯 HTTP 抓取, 整条链路不会因此挂掉
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..errors import IntelliSearchError, UpstreamError
from .ua import UAPool

log = logging.getLogger("intellisearch.renderer")

# Chromium 遇到主文档返回 4xx/5xx 时抛的网络错误码。
# 这类错误**不代表页面没内容** —— 文档已经提交, 服务器确实把正文发过来了
# (403 拦截页也是有正文的)。把它当成致命错误会让"被拦截 → 渲染救援"
# 这条唯一有价值的通道在最该起作用的场景下必然失败。
HTTP_STATUS_ERRORS = ("ERR_HTTP_RESPONSE_CODE_FAILURE", "ERR_TOO_MANY_REDIRECTS")


def _is_http_status_error(exc) -> bool:
    """判断导航异常是否只是"服务器回了错误状态码"(而非真的网络失败)。"""
    msg = str(exc)
    return any(code in msg for code in HTTP_STATUS_ERRORS)


# Chromium/Edge 自带错误页的特征(服务器没给正文时浏览器生成的那张)
_BROWSER_ERROR_MARKERS = ("neterror", "reload-button", "errorpagecontainer")


def _is_browser_error_page(html: str) -> bool:
    """判断一段 HTML 是不是浏览器自己的错误页, 而不是站点内容。"""
    head = (html or "")[:20000].lower()
    return any(m in head for m in _BROWSER_ERROR_MARKERS)

# 反检测启动参数: 抹掉自动化指纹, 贴近真实浏览器
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]

# 默认不加载的资源类型(渲染文本不需要, 且能大幅省流量提速)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


def _in_event_loop() -> bool:
    """当前线程是否运行在 asyncio 事件循环内(Playwright 同步 API 禁用)。"""
    import asyncio
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def box_done_wait(box: Dict[str, Any], timeout: float) -> bool:
    """等待工作线程回填结果。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "value" in box or "error" in box:
            return True
        time.sleep(0.01)
    return "value" in box or "error" in box


@dataclass
class RenderedPage:
    """渲染结果。字段与 FetchResponse 对齐, 便于上层统一处理。"""

    url: str = ""                    # 最终 URL(跟随跳转)
    status_code: int = 200
    html: str = ""
    title: str = ""
    text: str = ""                   # 渲染后的可见文本
    elapsed_ms: int = 0
    blocked_resources: int = 0       # 被拦截的资源数(评估渲染成本)
    js_errors: List[str] = field(default_factory=list)
    engine: str = ""                 # 实际使用的内核标识
    from_cache: bool = False
    selector_found: Optional[bool] = None   # 指定了 wait_selector 时是否命中
    scrolled: bool = False

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and bool(self.html)


class RendererUnavailable(IntelliSearchError):
    """渲染器不可用(未装 playwright 或找不到内核)。属于可降级情形。"""

    code = "RENDERER_UNAVAILABLE"


class BrowserRenderer:
    """Chromium 渲染器 —— 单工作线程模型(真正线程安全)。

    为什么不用"线程池 + 池化 browser":
    Playwright 的同步 API 实例**绑定创建它的线程**, 跨线程调用会抛
    "It looks like you are using Playwright Sync API inside the asyncio loop"
    或 Greenlet 错误, 表现为并发请求随机失败。
    因此这里改为「单一专用工作线程独占 Playwright + 请求队列」:
    - 所有 render() 调用把任务投进队列, 由唯一的工作线程串行执行;
    - 浏览器进程在队列首次消费时启动, 之后长期复用(避免每次冷启动);
    - 队列容量 = 并发上限, 超出则调用方阻塞等待, 天然限流;
    - 每次渲染仍新建 BrowserContext, cookie/存储互不污染。

    代价是渲染在单线程内串行, 但页面渲染本身以 I/O 等待为主
    (网络 + JS 执行均会释放 GIL), 实测吞吐足够; 如需更高并发,
    可增加工作线程数(每个线程一套 Playwright 实例)。
    """

    def __init__(self, headless: bool = True, pool_size: int = 2,
                 timeout: float = 30.0, block_media: bool = True,
                 ua_profile: str = "pc", nav_timeout: float = 25.0,
                 wait_ms: int = 800, executable_path: str = None,
                 proxy: str = None):
        self.headless = headless
        self.pool_size = max(1, pool_size)
        self.timeout = timeout
        self.nav_timeout = nav_timeout
        self.block_media = block_media
        self.wait_ms = wait_ms
        self.executable_path = executable_path or os.getenv("IS_CHROME_PATH", "")
        self.proxy = proxy
        self.ua = UAPool(profile=ua_profile, rotate=True)
        self.stats = {"rendered": 0, "failed": 0, "timeouts": 0,
                      "blocked_resources": 0, "js_errors": 0, "total_ms": 0}

        self._playwright = None
        self._pool: List[Any] = []            # 可借出的 browser
        self._all: List[Any] = []             # 全部已启动的 browser(关闭时统一清理)
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(self.pool_size)
        self._initialized = False
        self._init_error: str = ""
        self._engine_name = ""

        # 单工作线程: 独占 Playwright 实例, 所有渲染任务在此串行执行
        self._queue: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stopping = False
        # 等待预算上限(秒): 队列深度会放大预算, 但设顶防止真正卡死时无限等
        self.queue_budget_cap = float(os.getenv("IS_RENDER_QUEUE_BUDGET", "180"))

    # ------------------------------------------------------------------
    # 内核发现(委托 browser.py, 避免两套优先级)
    # ------------------------------------------------------------------
    def _discover_executable(self) -> Tuple[str, str]:
        """返回 (可执行文件路径, 来源标识)。找不到时路径为空串。

        委托给 browser.py 的候选发现 —— 那里是唯一的发现逻辑入口,
        本方法只做"取第一个能用的"这一层, 避免两套优先级不一致
        (曾出现 browser.py 选中系统 Edge、renderer.py 选中 playwright 缓存
        的分裂, 导致 /stats 报的引擎与实际启动的内核对不上)。
        """
        if self.executable_path and os.path.exists(self.executable_path):
            # 已探测到内核, 保留最初的来源标识(避免二次探测把来源覆盖成 env)
            return self.executable_path, (self._engine_name or "env")
        try:
            import playwright  # noqa: F401
        except ImportError:
            return "", ""
        try:
            from .browser import _candidates
        except Exception:                       # noqa: BLE001
            return "", ""
        for c in _candidates(self.executable_path or ""):
            if c.executable_path and os.path.exists(c.executable_path):
                return c.executable_path, c.source
        return "", ""

    def available(self) -> bool:
        """渲染器是否可用(不实际启动浏览器, 成本极低)。"""
        if self._initialized:
            return True
        if self._init_error:
            return False
        try:
            import playwright  # noqa: F401
        except ImportError:
            self._init_error = "未安装 playwright(可选依赖)"
            return False
        exe, src = self._discover_executable()
        if not exe:
            self._init_error = ("未找到 Chromium 内核。请执行 "
                                "`python -m playwright install chromium`，"
                                "或设置环境变量 IS_CHROME_PATH 指向本机 Chrome/Edge")
            return False
        self.executable_path = exe
        self._engine_name = src
        return True

    @property
    def engine(self) -> str:
        return self._engine_name or "chromium"

    def unavailable_reason(self) -> str:
        return self._init_error

    # ------------------------------------------------------------------
    # 工作线程(Playwright 实例的独占线程)
    # ------------------------------------------------------------------
    def _ensure_worker(self) -> None:
        """启动专用工作线程。渲染任务全部投递到这里执行。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            # 丢弃上一轮 close() 可能残留的 None 哨兵。
            # 否则旧线程没来得及消费它, 新线程一启动就 get 到哨兵并立刻退出,
            # 导致 close() 后第一次 render() 永远没人执行、干等到超时。
            self._drain_sentinels()
            self._stopping = False
            self._worker = threading.Thread(
                target=self._worker_loop, name="is-chromium", daemon=True)
            self._worker.start()

    def _drain_sentinels(self) -> None:
        """清空队列中残留的关闭哨兵。

        只丢弃 None; 万一遇到真实任务(理论上 close 时不应有)则放回队列,
        绝不静默吞掉调用方的请求。
        """
        pending = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                pending.append(item)
        for item in pending:
            self._queue.put(item)

    def _worker_loop(self) -> None:
        """工作线程主循环: 惰性初始化 Playwright, 串行消费渲染任务。

        退出时在**本线程内**关闭 Playwright —— 同步 API 的清理也必须
        与创建它的线程一致。
        """
        try:
            while not self._stopping:
                try:
                    item = self._queue.get(timeout=0.3)
                except queue.Empty:
                    continue
                if item is None:                   # 关闭信号
                    self._queue.task_done()
                    break
                fn, box = item
                try:
                    self._ensure_init()            # 只在本线程初始化
                    box["value"] = fn()
                except BaseException as e:          # noqa: BLE001
                    box["error"] = e
                finally:
                    self._queue.task_done()
        finally:
            try:
                self._shutdown_playwright()
            except Exception:                       # noqa: BLE001
                pass
            self._initialized = False

    def _submit(self, fn, timeout: float):
        """把任务投给工作线程并等待结果。

        注意预算计算: 工作线程串行消费, 排在前面���个任务各自最多占用
        自己的超时, 因此等待预算必须按队列深度放大, 否则后面的任务会在
        还没轮到执行时就"超时"。设上限防止真正卡死时无限等待。
        """
        self._ensure_worker()
        ahead = max(0, self._queue.qsize())
        budget = min(timeout * (ahead + 1) + 15.0, self.queue_budget_cap)
        box: Dict[str, Any] = {}
        self._queue.put((fn, box))
        if not box_done_wait(box, budget):
            raise UpstreamError(
                f"渲染队列等待超时(排队 {ahead} 个, 预算 {budget:.0f}s)",
                source="renderer")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def _ensure_init(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            if not self.available():
                raise RendererUnavailable(self._init_error or "渲染器不可用")
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            try:
                self._launch_browser()
            except Exception as e:              # noqa: BLE001
                self._shutdown_playwright()
                self._init_error = f"浏览器启动失败: {type(e).__name__}: {e}"
                raise RendererUnavailable(self._init_error)
            self._initialized = True
            log.info("渲染器就绪: engine=%s exe=%s pool=%d",
                     self._engine_name, self.executable_path, self.pool_size)

    def _launch_browser(self) -> None:
        args = list(STEALTH_ARGS)
        if os.name != "nt":
            # 容器内以非 root 运行时 chromium sandbox 不可用, 且 /dev/shm
            # 默认只有 64MB, 不关掉会随机崩页
            args += ["--no-sandbox", "--disable-dev-shm-usage"]
        kwargs: Dict[str, Any] = {
            "headless": self.headless,
            "args": args,
        }
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        if self.proxy:
            kwargs["proxy"] = {"server": self.proxy}
        b = self._playwright.chromium.launch(**kwargs)
        self._all.append(b)
        self._pool.append(b)

    def _acquire(self):
        self._sem.acquire()
        try:
            self._ensure_init()
            with self._lock:
                if self._pool:
                    return self._pool.pop()
            self._launch_browser()          # 池空则扩容
            with self._lock:
                return self._pool.pop()
        except Exception:
            self._sem.release()
            raise

    def _release(self, browser) -> None:
        try:
            with self._lock:
                self._pool.append(browser)
        finally:
            self._sem.release()

    def _shutdown_playwright(self) -> None:
        for b in self._all:
            try:
                b.close()
            except Exception:              # noqa: BLE001
                pass
        self._all.clear()
        self._pool.clear()
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:              # noqa: BLE001
                pass
            self._playwright = None

    def close(self, timeout: float = 15.0) -> None:
        """停止工作线程并关闭浏览器。

        关键约束: Playwright 的同步 API 必须在**创建它的线程**里清理。
        工作线程的 finally 块已经负责这件事, 所以这里只在确认线程确实
        已经退出后才做兜底清理 —— 若 join 超时(线程卡在某个页面上),
        绝不能从调用方线程去 close browser / stop playwright,
        那正是本模块要避免的跨线程反模式。
        """
        with self._lock:
            worker = self._worker
            self._worker = None
            self._stopping = True
        if worker is not None and worker.is_alive():
            self._queue.put(None)
            worker.join(timeout=timeout)
            if worker.is_alive():
                # 线程仍卡住: 交由它自己的 finally 清理, 调用方不越权。
                # 同时把残留哨兵清掉, 保证后续 render() 能拉起新线程。
                log.warning("渲染工作线程 %s 秒内未退出, 跳过跨线程清理",
                            timeout)
                self._drain_sentinels()
                self._initialized = False
                return
        # 线程已确认退出(或从未启动): 兜底清理是安全的
        self._shutdown_playwright()
        self._initialized = False
        self._drain_sentinels()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def render(self, url: str, wait_until: str = "domcontentloaded",
               wait_ms: int = None, wait_selector: str = None,
               scroll: bool = False, timeout: float = None,
               headers: Dict[str, str] = None,
               cookies: list = None) -> RenderedPage:
        """加载并渲染页面, 返回渲染后的 DOM(线程安全入口)。

        任务被投递到专用工作线程执行, 因此本方法可从任意线程、
        甚至 asyncio 事件循环内安全调用。

        wait_until: playwright 的加载策略(domcontentloaded/load/networkidle)
        wait_ms:    额外固定延时(毫秒), 处理慢渲染的 SPA
        wait_selector: 等待某元素出现(SPA 常用)
        scroll:     是否滚动到底部触发懒加载
        """
        budget = (timeout or self.nav_timeout) + 25.0

        def _job():
            return self._render_once(url, wait_until, wait_ms, wait_selector,
                                     scroll, timeout, headers, cookies)

        if _in_event_loop():
            # 事件循环内: 交给工作线程(其内部本就无事件循环)
            return self._submit(_job, budget)
        # 普通线程: 若已在工作线程则直接执行, 否则投递
        if self._worker is not None and threading.current_thread() is self._worker:
            return _job()
        return self._submit(_job, budget)

    def _render_once(self, url: str, wait_until: str = "domcontentloaded",
                     wait_ms: int = None, wait_selector: str = None,
                     scroll: bool = False, timeout: float = None,
                     headers: Dict[str, str] = None,
                     cookies: list = None) -> RenderedPage:
        """真正的渲染实现。只允许在工作线程内调用。"""
        nav_timeout = (timeout or self.nav_timeout) * 1000
        extra_wait = self.wait_ms if wait_ms is None else wait_ms
        browser = self._acquire()
        t0 = time.time()
        context = None
        page = None
        js_errors: List[str] = []
        blocked = {"n": 0}
        selector_found: Optional[bool] = None
        try:
            ctx_args: Dict[str, Any] = {
                "user_agent": self.ua.get(),
                "locale": "zh-CN" if not headers else
                           headers.get("Accept-Language", "zh-CN"),
                "viewport": {"width": 1366, "height": 768},
                "java_script_enabled": True,
                "ignore_https_errors": True,
            }
            if self.proxy:
                ctx_args["proxy"] = {"server": self.proxy}
            context = browser.new_context(**ctx_args)
            # 给本次渲染设一个**总截止时间**。
            # content() / evaluate() / title() 都不接受 timeout 参数,
            # 若页面 JS 死循环或 DOM 巨大, 这些调用可以无限期挂住 ——
            # 而本渲染器是单工作线程, 一个任务挂住就等于整个渲染器楔死。
            # context.set_default_timeout 能兜住该上下文内的所有操作。
            deadline_ms = (timeout or self.nav_timeout) * 1000 + (
                extra_wait or 0) + 15000
            context.set_default_timeout(deadline_ms)
            # 反检测: 从页面层面抹掉 webdriver 痕迹
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            page = context.new_page()
            page.on("pageerror", lambda e: js_errors.append(str(e)[:200]))

            if self.block_media:
                def _route(route):
                    try:
                        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                            blocked["n"] += 1
                            route.abort()
                        else:
                            route.continue_()
                    except Exception:          # noqa: BLE001
                        pass
                page.route("**/*", _route)

            if headers:
                # 过滤掉不属于导航头的字段
                nav_headers = {k: v for k, v in headers.items()
                               if k.lower() in ("accept-language", "referer",
                                                "cookie", "dnt", "upgrade-insecure-requests")}
                if nav_headers:
                    page.set_extra_http_headers(nav_headers)
            if cookies:
                context.add_cookies(cookies)

            # 主文档状态码: goto 抛异常时 resp 为 None, 靠监听器兜底
            doc_status: List[int] = []

            def _on_response(r):
                try:
                    if getattr(r.request, "resource_type", "") == "document":
                        doc_status.append(r.status)
                except Exception:                # noqa: BLE001
                    pass
            page.on("response", _on_response)

            resp = None
            nav_error = ""
            try:
                resp = page.goto(url, wait_until=wait_until, timeout=nav_timeout)
            except Exception as nav_exc:        # noqa: BLE001
                # 主文档返回 4xx/5xx **且响应体为空**时 Chromium 会抛
                # net::ERR_HTTP_RESPONSE_CODE_FAILURE(有正文时它并不抛, 会正常
                # 返回 Response)。所以走到这里意味着: 服务器没给正文, 浏览器
                # 只能渲染出自己的错误页 —— 那不是我们要的内容, 必须按失败处理
                # (见下方校验), 否则上层会拿到 300KB 的错误页并报 ok=True。
                if not _is_http_status_error(nav_exc):
                    raise
                nav_error = str(nav_exc)[:200]
                log.debug("导航返回错误状态码(%s), 校验是否为有效文档", nav_error[:80])
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector,
                                           timeout=min(8000, nav_timeout))
                    selector_found = True
                except Exception:              # noqa: BLE001
                    selector_found = False
                    log.debug("等待选择器 %s 超时(继续)", wait_selector)
            if extra_wait:
                page.wait_for_timeout(int(extra_wait))
            scrolled = False
            if scroll:
                self._auto_scroll(page)
                scrolled = True

            html = page.content()
            status = (resp.status if resp is not None
                      else (doc_status[-1] if doc_status else 0))
            if resp is None:
                # goto 抛错 = 服务器没给可用正文。此时 html 是 Chromium/Edge
                # 自己生成的错误页(含 neterror / reload-button), 绝不能当正文
                # 交上去; 重定向链的最后一个状态码才是真实状态(取首个会拿到
                # 中间的 301/302, 而 3xx 会被上层判成 ok)。
                if (not status or status >= 300
                        or _is_browser_error_page(html)):
                    raise UpstreamError(
                        f"页面加载失败 status={status} {nav_error[:80]}",
                        source="renderer")
            try:
                text = page.evaluate(
                    "() => document.body ? document.body.innerText : ''")
            except Exception:                  # noqa: BLE001
                text = ""
            result = RenderedPage(
                url=page.url,
                status_code=status,
                html=html,
                title=(page.title() or "")[:300],
                text=text or "",
                elapsed_ms=int((time.time() - t0) * 1000),
                blocked_resources=blocked["n"],
                js_errors=js_errors[:5],
                engine=self.engine,
                selector_found=selector_found,
                scrolled=scrolled,
            )
            self.stats["rendered"] += 1
            self.stats["blocked_resources"] += blocked["n"]
            self.stats["js_errors"] += len(js_errors)
            self.stats["total_ms"] += result.elapsed_ms
            return result
        except Exception as e:                  # noqa: BLE001
            self.stats["failed"] += 1
            name = type(e).__name__
            msg = str(e)
            if "Timeout" in name or "timeout" in msg.lower():
                self.stats["timeouts"] = self.stats.get("timeouts", 0) + 1
                raise UpstreamError(f"渲染超时({msg[:120]})", source="renderer")
            raise UpstreamError(f"渲染失败 {name}: {msg[:200]}", source="renderer")
        finally:
            for obj in (page, context):
                try:
                    if obj:
                        obj.close()
                except Exception:              # noqa: BLE001
                    pass
            self._release(browser)

    @staticmethod
    def _auto_scroll(page, steps: int = 6, pause: int = 250) -> None:
        """滚动到底部, 触发懒加载内容。"""
        try:
            for i in range(steps):
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(pause)
        except Exception:                      # noqa: BLE001
            pass

    def info(self) -> Dict[str, Any]:
        return {
            "available": self.available(),
            "engine": self.engine,
            "executable": self.executable_path,
            "headless": self.headless,
            "pool_size": self.pool_size,
            "initialized": self._initialized,
            "unavailable_reason": self._init_error,
            "stats": dict(self.stats),
        }


# ----------------------------------------------------------------------
# 渲染必要性判定
# ----------------------------------------------------------------------
# SPA 框架的挂载点/数据标记: 出现这些说明正文是 JS 渲染出来的
_SPA_MARKERS_PLAIN = (
    'id="root"', "id='root'", 'id="app"', "id='app'",
    "__next_data__", "__nuxt__", "window.__initial_state__",
    "data-reactroot", "ng-version", "v-cloak",
    # <noscript> 里的"请开启 JavaScript"提示是 SPA 通用形态
    "<noscript",
)

_BLOCK_MARKERS = re.compile(
    r"(captcha|安全验证|人机验证|请完成安全验证|unusual traffic|are you a human"
    r"|robot check|访问验证|检测到异常访问|请输入验证码)", re.I)

# "正在加载"占位页: 整页可见文字就这一句, 说明正文由 JS 异步填充
_PLACEHOLDER_RE = re.compile(
    r"^\s*(loading|加载中|请稍候|Just a moment|Checking your browser"
    r"|请开启 ?JavaScript|JavaScript is required)[\s.…]*$", re.I)


def needs_render(html: str, status_code: int = 200,
                 min_text: int = 1200) -> bool:
    """判断该页面是否必须用浏览器渲染 —— 全项目唯一的判定入口。

    覆盖四类情况:
    1. HTTP 层就被挡(403/429/5xx)
    2. 命中验证码 / 安全验证特征
    3. 典型 SPA 挂载点存在, 但可见文本极少
    4. **文本密度过低**: 可见文字少而 HTML 相对文本极重。这类页面往往没有
       任何框架标记(东方财富行情页就是), 正文靠 JS 异步拉取, HTTP 直取只能
       看到一屏导航 —— 是最容易被漏判的一类, 因此阈值单独放宽。

    阈值刻意偏松: 宁可多渲染一次(多花 ~2 秒), 也不要把只有导航菜单的空正文
    当作正常内容交给下游。
    """
    if status_code and (status_code in (403, 429) or 500 <= status_code < 600):
        return True
    if not html:
        return True
    head = html[:20000]
    if _BLOCK_MARKERS.search(head) and len(html) < 20000:
        return True

    # SPA 挂载点/框架标记: 不受页面长度限制, 短小的骨架页同样要渲染
    low = head.lower()
    if any(m in low for m in _SPA_MARKERS_PLAIN):
        return True

    # 极短页面 + 命中"加载中"占位提示
    if len(html) < 3000:
        text = re.sub(r"(?s)<[^>]+>", " ", head)
        if _PLACEHOLDER_RE.match(text.strip()[:120] or "x"):
            return True

    # 文本密度判据(只对有实际体积的页面有意义)
    if len(html) > 1500:
        text = re.sub(r"(?s)<(script|style|noscript)[^>]*>.*?</\1>", " ", head)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        n = len(re.sub(r"\s+", " ", text).strip())
        if n < 400 and len(html) > 3 * max(n, 1):
            return True
        if n < min_text and len(html) > 8 * max(n, 1):
            return True
    return False
