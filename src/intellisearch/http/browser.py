"""Chromium 内核发现与浏览器池 —— 渲染能力的入口层。

本模块职责:
1. find_chromium(): 发现本机可用的 Chromium 内核(完全本地, 无需任何 API Key);
2. BrowserPool: 池化浏览器进程 + 每次渲染独立 BrowserContext, 供 SmartFetcher 调度;
3. get_pool(): 按配置返回进程级共享的单例池。

为什么需要"发现"而不是直接用 playwright 默认值:
- Windows 上 Chromium 有多个发行渠道(Chrome / Edge / Chromium), 路径与版本各异;
- playwright 库期望的浏览器版本号会随升级变动(实测本机 playwright 1.63
  期望 chromium-1243, 而本地缓存只有 chromium-1210, 直接 launch 会报
  "Executable doesn't exist"), 而完整版内核其实还在, 只是版本号对不上。
因此这里以"可用性"而非"版本号"来挑内核, 每种候选都做真实启动探测。

内核来源优先级(全部本地, 零外部依赖, 零 API Key):
    1. 显式配置 browser_path / IS_BROWSER_PATH / IS_CHROME_PATH
    2. **随包分发的内置 chromium**(playwright 自带, 见 scripts/install_browser.py)
    3. ms-playwright 缓存目录中任意版本的完整 chromium
    4. 系统浏览器(Edge / Chrome / Chromium / Brave) —— 仅作兜底

内置化说明: 执行 ``python scripts/install_browser.py`` 后, chromium 落在
site-packages/playwright/driver/package/.local-browsers/ 下, 随 venv 一起
分发到任何机器, 不要求目标机器预装浏览器。目标机器若无内置内核, 仍会
自动回退到系统浏览器, 行为与旧版完全一致。
"""
from __future__ import annotations

import glob
import os
import platform
import shutil
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import Config, DEFAULT_CONFIG
from ..errors import IntelliSearchError, UpstreamError

# ----------------------------------------------------------------------
# 异常
# ----------------------------------------------------------------------
class RenderError(IntelliSearchError):
    """渲染过程失败(超时/内核崩溃/页面异常)。属于可降级情形。"""

    code = "RENDER_ERROR"


class BrowserUnavailable(IntelliSearchError):
    """找不到可用内核, 或浏览器无法启动。属于可降级情形。"""

    code = "BROWSER_UNAVAILABLE"


# ----------------------------------------------------------------------
# 内核发现
# ----------------------------------------------------------------------
@dataclass
class BrowserCandidate:
    """一个候选内核。

    kind="path"     -> 用 executable_path 启动(已定位到具体文件)
    kind="channel"  -> 让 playwright 按 channel 自行定位(如 msedge / chrome)
    """
    kind: str
    name: str
    executable_path: Optional[str] = None
    channel: Optional[str] = None
    source: str = ""

    def launch_kwargs(self) -> Dict[str, Any]:
        if self.kind == "channel":
            return {"channel": self.channel}
        return {"executable_path": self.executable_path}

    def describe(self) -> str:
        if self.kind == "channel":
            return f"{self.name}[channel={self.channel}]"
        return f"{self.name}[{self.executable_path}]"


# Windows 上常见的 Chromium 系浏览器(相对各 Program Files 根目录)
_WIN_PAIRS = [
    ("Microsoft Edge", "msedge", "Microsoft/Edge/Application/msedge.exe"),
    ("Google Chrome", "chrome", "Google/Chrome/Application/chrome.exe"),
    ("Google Chrome Beta", "chrome", "Google/Chrome Beta/Application/chrome.exe"),
    ("Chromium", "chromium", "Chromium/Application/chrome.exe"),
    ("Brave", "brave", "BraveSoftware/Brave-Browser/Application/brave.exe"),
]
_WIN_ROOTS = [
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    os.path.expandvars(r"%LOCALAPPDATA%"),
    os.path.expandvars(r"%PROGRAMFILES%"),
    os.path.expandvars(r"%PROGRAMFILES(X86)%"),
]

# Linux / macOS
_UNIX_PAIRS = [
    ("Google Chrome", "chrome", "google-chrome"),
    ("Google Chrome", "chrome", "google-chrome-stable"),
    ("Microsoft Edge", "msedge", "microsoft-edge"),
    ("Microsoft Edge", "msedge", "microsoft-edge-stable"),
    ("Chromium", "chromium", "chromium"),
    ("Chromium", "chromium", "chromium-browser"),
    ("Brave", "brave", "brave-browser"),
]
_MAC_APPS = [
    ("Google Chrome", "chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("Microsoft Edge", "msedge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ("Chromium", "chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium"),
]


def _roots() -> List[str]:
    """ms-playwright 浏览器缓存目录的全部可能位置。

    注意: Windows 上盘符可能重定向(桌面在 D 盘但 LOCALAPPDATA 仍在 C 盘),
    不能只信 expanduser("~"), 必须多候选一起扫。
    """
    out: List[str] = []
    env = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if env and env not in ("0", ""):
        out.append(env)
    local = os.getenv("LOCALAPPDATA")
    if local:
        out.append(os.path.join(local, "ms-playwright"))
    home = os.path.expanduser("~")
    if home:
        out.append(os.path.join(home, "AppData", "Local", "ms-playwright"))
        out.append(os.path.join(home, ".cache", "ms-playwright"))
        out.append(os.path.join(home, "Library", "Caches", "ms-playwright"))
    seen, res = set(), []
    for p in out:
        n = os.path.normpath(p)
        if n not in seen and os.path.isdir(n):
            seen.add(n)
            res.append(n)
    return res


# 完整版内核的相对路径(刻意不依赖版本号)
_CHROMIUM_GLOBS = [
    "chromium-*/chrome-win64/chrome.exe",
    "chromium-*/chrome-win/chrome.exe",
    "chromium-*/chrome-linux/chrome",
    "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
]


def _version_of(path: str) -> int:
    import re
    m = re.search(r"chromium-(\d+)", path)
    return int(m.group(1)) if m else 0


def _playwright_browsers() -> List[str]:
    """扫描 playwright 缓存里任意版本的完整 chromium(版本号大的优先)。"""
    found: List[str] = []
    for root in _roots():
        for pat in _CHROMIUM_GLOBS:
            for hit in glob.glob(os.path.join(root, pat)):
                if os.path.isfile(hit):
                    found.append(hit)
    found.sort(key=_version_of, reverse=True)
    return found


# 随包分发的内核, 相对 site-packages/playwright/driver/package/.local-browsers/
# 刻意用 glob 而非 playwright 自己的 executable_path:
#   playwright 只有在运行时也设了 PLAYWRIGHT_BROWSERS_PATH=0 才会去包目录找,
#   否则会报一个不存在的全局缓存路径。这里直接扫目录, 与环境变量无关。
_PKG_BROWSER_GLOBS = [
    # 完整内核(真实渲染用)
    "chromium-*/chrome-win64/chrome.exe",
    "chromium-*/chrome-win32/chrome.exe",
    "chromium-*/chrome-win/chrome.exe",
    "chromium-*/chrome-linux/chrome",
    "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    "chromium-*/chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
    # 精简 headless 内核(体积更小, 也能用)
    "chromium_headless_shell-*/chrome-win64/headless_shell.exe",
    "chromium_headless_shell-*/chrome-linux/headless_shell",
    "chromium_headless_shell-*/chrome-mac/headless_shell",
]


def _package_browsers() -> List[str]:
    """扫描 **包内** .local-browsers 目录下的内置 chromium(与环境变量无关)。"""
    try:
        import playwright
    except ImportError:                                  # noqa: F401
        return []
    import pathlib
    root = pathlib.Path(playwright.__file__).resolve().parent \
        / "driver" / "package" / ".local-browsers"
    if not root.is_dir():
        return []
    found: List[str] = []
    for pat in _PKG_BROWSER_GLOBS:
        for hit in root.glob(pat):
            if hit.is_file():
                found.append(str(hit))
    # 版本号大的优先(chromium-1243 > chromium-1210)
    found.sort(key=_version_of, reverse=True)
    return found


def _playwright_bundled() -> Optional[str]:
    """返回随包分发的内置 chromium 路径; 没有则返回 None。

    两段式:
      1. 直接扫包目录(不依赖 PLAYWRIGHT_BROWSERS_PATH, 最可靠);
      2. 兜底用 playwright 自己的 executable_path(仅当运行时也设了 =0 才有效)。
    """
    for p in _package_browsers():
        return p

    try:
        from playwright.sync_api import sync_playwright
    except Exception:                                    # noqa: BLE001
        return None

    def _get() -> Optional[str]:
        try:
            with sync_playwright() as p:
                return p.chromium.executable_path
        except Exception:                                # noqa: BLE001
            return None

    if not _in_event_loop():
        path = _get()
    else:
        box: Dict[str, Any] = {}
        t = threading.Thread(target=lambda: box.update(p=_get()),
                             name="is-bundled", daemon=True)
        t.start()
        t.join(timeout=15)
        path = box.get("p")
    return path if path and os.path.exists(path) else None


def find_chromium(explicit: str = "", probe: bool = False) -> Optional[str]:
    """定位一个可用的 Chromium 内核, 返回可执行文件路径; 找不到返回 None。

    probe=True 时逐个候选做真实启动验证(更准, 但每个候选约 1 秒)。

    注意: 候选可能是 "channel" 型(交给 playwright 按channel 自行定位),
    此时返回该channel 名 —— 调用方通常只需要知道"有内核可用",
    真正启动交给 BrowserPool。
    """
    for c in _candidates(explicit):
        if not probe:
            return c.executable_path or c.channel
        if _can_launch(c):
            return c.executable_path or c.channel
    if probe:
        return None
    cands = _candidates(explicit)
    return (cands[0].executable_path or cands[0].channel) if cands else None


def _candidates(explicit: str = "") -> List[BrowserCandidate]:
    """按"可移植性"排序候选内核。

    顺序: 显式指定 → **playwright 自带 chromium** → ms-playwright 缓存 →
    系统浏览器。

    为什么把自带内核提到系统浏览器之前:
        自带内核随包安装(PLAYWRIGHT_BROWSERS_PATH=0 时落在 site-packages 内),
        行为在所有机器上一致; 而系统 Edge/Chrome 各版本差异很大, 换台机器
        可能就没有, 或版本与 playwright 不兼容。要让"装完就能跑"成立,
        应当优先用包内那份。
    """
    out: List[BrowserCandidate] = []
    seen = set()

    def add(c: BrowserCandidate):
        key = (c.kind, c.executable_path or c.channel)
        if key not in seen:
            seen.add(key)
            out.append(c)

    # 1) 显式指定(最高优先级: 用户明确要求)
    for p in (explicit, os.getenv("IS_BROWSER_PATH"), os.getenv("IS_CHROME_PATH")):
        if p and os.path.exists(p):
            add(BrowserCandidate(kind="path", name="Chromium(指定)",
                                 executable_path=os.path.abspath(p), source="config"))

    # 2) playwright 自带 chromium —— 随包安装, 跨机器行为一致
    b = _playwright_bundled()
    if b:
        add(BrowserCandidate(kind="path", name="Chromium(内置)",
                             executable_path=b, source="playwright-bundled"))

    # 3) ms-playwright 缓存里任意版本的完整 chromium
    for p in _playwright_browsers():
        add(BrowserCandidate(kind="path", name="Chromium(缓存)",
                             executable_path=p, source="ms-playwright-cache"))

    # 4) 系统浏览器(兜底: 上面都没有时, 复用用户已装的)
    if sys.platform == "win32":
        for name, ch, rel in _WIN_PAIRS:
            for root in _WIN_ROOTS:
                p = os.path.join(root, rel.replace("/", os.sep))
                if os.path.isfile(p):
                    # 系统浏览器一律用 path 启动: 不依赖 playwright 是否认识该
                    # channel, 也不受版本匹配限制
                    add(BrowserCandidate(kind="path", name=name,
                                         executable_path=p, source="system"))
    elif sys.platform == "darwin":
        for name, ch, p in _MAC_APPS:
            if os.path.exists(p):
                add(BrowserCandidate(kind="path", name=name,
                                     executable_path=p, source="system"))
        for name, ch, exe in _UNIX_PAIRS:
            w = shutil.which(exe)
            if w:
                add(BrowserCandidate(kind="path", name=name,
                                     executable_path=w, source="which"))
    else:
        for name, ch, exe in _UNIX_PAIRS:
            w = shutil.which(exe)
            if w:
                add(BrowserCandidate(kind="path", name=name,
                                     executable_path=w, source="which"))
    return out


# 容器/低配环境的安全启动参数
_LAUNCH_ARGS = [
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--no-first-run",
    "--no-default-browser-check",
    "--mute-audio",
]


def _can_launch(candidate: BrowserCandidate, timeout_ms: int = 25000) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:                                    # noqa: BLE001
        return False

    def _probe() -> bool:
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True, timeout=timeout_ms,
                                      args=list(_LAUNCH_ARGS),
                                      **candidate.launch_kwargs())
                b.close()
            return True
        except Exception:                                # noqa: BLE001
            return False

    if not _in_event_loop():
        return _probe()
    # 事件循环内: 换到独立线程探测
    box: Dict[str, Any] = {}
    t = threading.Thread(target=lambda: box.update(r=_probe()),
                         name="is-probe", daemon=True)
    t.start()
    t.join(timeout=(timeout_ms / 1000.0) + 10)
    return bool(box.get("r"))


def _in_event_loop() -> bool:
    """当前线程是否运行在 asyncio 事件循环中。

    Playwright 同步 API 在事件循环内调用会直接报错, 必须规避。
    """
    import asyncio
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _is_timeout(exc: BaseException) -> bool:
    """判断异常是否为超时(用于 stats["timeouts"] 计数)。"""
    name = type(exc).__name__
    if "Timeout" in name:
        return True
    msg = str(exc) + str(getattr(exc, "message", "") or "")
    return "timeout" in msg.lower() or "超时" in msg


def _normalize_engine(path: str) -> str:
    """把可执行文件路径归一化成浏览器家族名(便于跨机器对比 /stats 输出)。

    注意匹配顺序: playwright 缓存里的完整内核路径形如
    ``.../chromium-1210/chrome-win64/chrome.exe``, 同时含 "chromium" 和 "chrome",
    因此必须先判chromium 再判 chrome, 否则会误报成 chrome。
    """
    p = (path or "").lower().replace("\\", "/")
    for key, name in (("msedge", "msedge"), ("edge", "msedge"),
                      ("brave", "brave"),
                      ("chromium", "chromium"), ("chrome", "chrome")):
        if key in p:
            return name
    return "chromium"


# ----------------------------------------------------------------------
# 渲染请求 / 结果
# ----------------------------------------------------------------------
@dataclass
class RenderRequest:
    """一次渲染任务的参数。"""
    url: str
    timeout: float = 25.0
    wait_until: str = "domcontentloaded"
    wait_selector: str = ""
    settle_ms: int = 0
    scroll: bool = False
    lang: str = "zh"
    block_resources: bool = True
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: List[Dict] = field(default_factory=list)


@dataclass
class RenderResult:
    """渲染结果。字段与 FetchResponse 对齐, 便于上层统一消费。"""
    url: str = ""
    final_url: str = ""
    status: int = 200
    html: str = ""
    text: str = ""
    title: str = ""
    engine: str = ""
    elapsed_ms: int = 0
    blocked_requests: int = 0
    js_errors: List[str] = field(default_factory=list)
    selector_found: Optional[bool] = None   # 指定了 wait_selector 时是否命中
    scrolled: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.html)


# ----------------------------------------------------------------------
# 浏览器池
# ----------------------------------------------------------------------
class BrowserPool:
    """池化的 Chromium 渲染池(进程级共享)。

    模型: 浏览器进程昂贵 -> 池化复用; BrowserContext 很轻 -> 每次渲染新建,
    避免 cookie / localStorage 串味。并发上限由 max_pages 控制。
    """

    _instances: Dict[int, "BrowserPool"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, config: Config = None, max_pages: int = 4,
                 headless: bool = True, browser_path: str = "",
                 proxy: str = "", pool_size: int = 1):
        self.cfg = config or DEFAULT_CONFIG
        self.max_pages = max(1, int(max_pages))
        self.headless = headless
        self.browser_path = browser_path or getattr(self.cfg, "browser_path", "")
        self.proxy = proxy
        self.pool_size = max(1, pool_size)

        self._renderer = None
        self._renderer_lock = threading.Lock()
        self._sem = threading.Semaphore(self.max_pages)
        self._engine_name = ""
        self._init_error = ""
        self.stats = {"rendered": 0, "failed": 0, "timeouts": 0,
                      "blocked_requests": 0, "total_ms": 0}

    # ---------- 内核 ----------
    def _ensure_renderer(self):
        if self._renderer is not None:
            return self._renderer
        with self._renderer_lock:
            if self._renderer is not None:
                return self._renderer
            try:
                import playwright  # noqa: F401
            except ImportError:
                self._init_error = ("未安装 playwright(可选依赖)。"
                                    "执行 pip install playwright")
                raise BrowserUnavailable(self._init_error)

            from .renderer import BrowserRenderer
            r = BrowserRenderer(headless=self.headless,
                                pool_size=self.pool_size,
                                block_media=True,
                                executable_path=self.browser_path or None,
                                proxy=self.proxy or None)
            if not r.available():
                self._init_error = r.unavailable_reason() or "未找到 Chromium 内核"
                raise BrowserUnavailable(self._init_error)
            self._renderer = r
            self._engine_name = _normalize_engine(r.executable_path)
            return r

    def available(self) -> bool:
        """是否具备渲染能力(不真正启动浏览器, 成本极低)。"""
        if self._renderer is not None:
            return True
        if self._init_error:
            return False
        if _in_event_loop():
            # 事件循环内不能直接用同步 API, 交给工作线程判定
            box: Dict[str, Any] = {}
            t = threading.Thread(target=lambda: box.update(r=self._available_sync()),
                                 name="is-avail", daemon=True)
            t.start()
            t.join(timeout=20)
            return bool(box.get("r"))
        return self._available_sync()

    def _available_sync(self) -> bool:
        try:
            self._ensure_renderer()
            return True
        except BrowserUnavailable as e:
            self._init_error = e.message
            return False
        except Exception as e:                           # noqa: BLE001
            self._init_error = f"{type(e).__name__}: {e}"
            return False

    def engine_name(self) -> str:
        """当前使用的内核标识(供 /stats 观测)。

        返回归一化的浏览器家族名(msedge / chrome / chromium / brave / ...),
        而不是完整路径 —— 便于跨机器对比。
        """
        if not self._engine_name:
            try:
                self._ensure_renderer()
            except Exception:                            # noqa: BLE001
                return "none"
        return self._engine_name or "chromium"

    @property
    def engine_detail(self) -> str:
        """内核来源 + 可执行文件路径(排障用)。"""
        r = self._renderer
        return f"{self.engine_name()}[{getattr(r, 'executable_path', '') or ''}]"

    def unavailable_reason(self) -> str:
        return self._init_error

    # ---------- 渲染 ----------
    def render(self, req: RenderRequest) -> RenderResult:
        """执行渲染。失败抛 RenderError(可降级)。

        实现要点: Playwright 的同步 API 禁止在 asyncio 事件循环内调用,
        而本工具可能被异步框架(如 FastAPI/uvicorn)调用方嵌入。
        因此检测到当前线程存在运行中的事件循环时, 把渲染任务投递到一个
        专用工作线程执行 —— 该线程没有事件循环, 同步 API 可正常使用。
        """
        if not req.url or not req.url.startswith(("http://", "https://")):
            raise RenderError(f"URL 非法: {req.url!r}")

        if _in_event_loop():
            return self._render_in_worker(req)
        return self.render_sync(req)

    def _render_in_worker(self, req: RenderRequest,
                          timeout_pad: float = 15.0) -> RenderResult:
        """在无事件循环的独立线程中执行渲染。"""
        box: Dict[str, Any] = {}

        def _run():
            try:
                box["result"] = self.render_sync(req)
            except BaseException as e:                # noqa: BLE001
                box["error"] = e

        t = threading.Thread(target=_run, name="is-render", daemon=True)
        t.start()
        # 超时兜底: 渲染本身有内部超时, 这里再多等一会儿防线程卡死。
        # 注意内部还有页签信号量 + 单工作线程两层排队, 排在后面的任务可能
        # 等超过一个自身超时, 故预算放大到 3 倍(约等于 3 个页签轮完)。
        budget = min((req.timeout or 25.0) * 3 + timeout_pad, 180.0)
        t.join(timeout=budget)
        if "error" in box:
            raise box["error"]
        if "result" not in box:
            raise RenderError(f"渲染线程超时({budget:.0f}s 未返回)", source="browser_pool")
        return box["result"]

    def render_sync(self, req: RenderRequest) -> RenderResult:
        """执行渲染。失败抛 RenderError(可降级)。"""
        if not req.url or not req.url.startswith(("http://", "https://")):
            raise RenderError(f"URL 非法: {req.url!r}")
        renderer = self._ensure_renderer()

        # 必须带超时: 若某个渲染任务卡死(页面阻塞/JS 死循环)且一直没有
        # 归还 permit, 无参 acquire() 会让第 N+1 个请求**永久阻塞** ——
        # 整个渲染器被楔死, 且无法降级。宁可超时抛错让上层降级。
        budget = (req.timeout or 25.0) * 3 + 15.0
        if not self._sem.acquire(timeout=budget):
            raise RenderError(f"等待渲染页签超时({budget:.0f}s, 上限 "
                              f"{self.max_pages})", source="browser_pool")
        try:
            page = renderer.render(
                req.url,
                wait_until=req.wait_until or "domcontentloaded",
                wait_ms=req.settle_ms or None,
                wait_selector=req.wait_selector or None,
                scroll=req.scroll,
                timeout=req.timeout or self.cfg.render_timeout,
                headers=req.headers or None,
                cookies=req.cookies or None,
            )
        except BrowserUnavailable:
            raise
        except (RenderError, UpstreamError) as e:
            self.stats["failed"] += 1
            if _is_timeout(e):
                self.stats["timeouts"] += 1
            raise RenderError(f"渲染失败: {e.message}", source="browser_pool")
        except Exception as e:                           # noqa: BLE001
            name = type(e).__name__
            self.stats["failed"] += 1
            if "Timeout" in name or "timeout" in str(e).lower():
                self.stats["timeouts"] += 1
                raise RenderError(f"渲染超时({str(e)[:120]})", source="browser_pool")
            raise RenderError(f"渲染异常 {name}: {str(e)[:200]}",
                              source="browser_pool")
        finally:
            self._sem.release()

        self.stats["rendered"] += 1
        self.stats["blocked_requests"] += page.blocked_resources
        self.stats["total_ms"] += page.elapsed_ms
        return RenderResult(
            url=req.url, final_url=page.url, status=page.status_code,
            html=page.html, text=page.text, title=page.title,
            engine=page.engine, elapsed_ms=page.elapsed_ms,
            blocked_requests=page.blocked_resources,
            js_errors=list(page.js_errors),
            selector_found=page.selector_found,
            scrolled=page.scrolled,
        )

    def close(self, timeout: float = 15.0) -> None:
        with self._renderer_lock:
            if self._renderer is not None:
                r, self._renderer = self._renderer, None
                try:
                    r.close(timeout=timeout)
                except Exception:                       # noqa: BLE001
                    pass

    def info(self) -> Dict[str, Any]:
        return {
            "available": self.available(),
            "engine": self.engine_name() if not self._init_error else "none",
            # 实际用的内核绝对路径: 排障时确认"用的是包内那份还是系统那份"
            "engine_detail": self.engine_detail if not self._init_error else "",
            "headless": self.headless,
            "max_pages": self.max_pages,
            "unavailable_reason": self._init_error,
            "stats": dict(self.stats),
        }


def get_pool(config: Config = None, max_pages: int = 4,
             headless: bool = True) -> BrowserPool:
    """进程级共享的浏览器池单例(避免重复启动浏览器进程)。"""
    cfg = config or DEFAULT_CONFIG
    key = id(cfg)
    with BrowserPool._instances_lock:
        pool = BrowserPool._instances.get(key)
        if pool is None:
            pool = BrowserPool(config=cfg, max_pages=max_pages, headless=headless)
            BrowserPool._instances[key] = pool
        return pool


def reset_pools() -> None:
    """关闭并清空所有池(测试与热切换用)。"""
    with BrowserPool._instances_lock:
        for p in BrowserPool._instances.values():
            p.close()
        BrowserPool._instances.clear()
