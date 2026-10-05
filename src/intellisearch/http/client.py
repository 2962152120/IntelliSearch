"""统一 HTTP 客户端。

职责:
- 连接复用 / 超时 / 指数退避重试 / 并发上限(全局 + 单主机)
- UA 轮换、代理池轮换
- 资源过滤(默认不下载图片/视频/字体/CSS)
- 反爬拦截识别(验证码 / 安全验证页) -> BlockedError
- 审计日志
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

import httpx

from ..config import Config, DEFAULT_CONFIG
from ..errors import (BlockedError, IntelliSearchError as IntelliSearchErrorBase,
                      TimeoutError_, UpstreamError)
from .ua import UAPool

log = logging.getLogger("intellisearch.http")

# 文本类 content-type 前缀白名单
TEXT_CT_PREFIXES = ("text/", "application/json", "application/xml",
                    "application/xhtml", "application/rss", "application/atom",
                    "application/javascript")
# 明确视为媒体的类型(默认不下载)
MEDIA_CT_PREFIXES = ("image/", "video/", "audio/", "font/", "application/font",
                     "application/octet-stream", "application/pdf")

# 只保留强特征词。注意不要放 "verify" / "access denied" 这类泛用词:
# 正常结果页的 JS 里到处是 verify/verified 字样, 会造成大量误判。
BLOCK_TOKENS = ("captcha", "安全验证", "人机验证", "请完成安全验证", "验证码",
                "unusual traffic", "are you a human", "robot check",
                "访问验证", "请求异常", "检测到异常访问")


@dataclass
class FetchResponse:
    url: str                     # 最终 URL(跟随跳转后)
    status_code: int
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    content_type: str = ""
    elapsed_ms: int = 0
    retries: int = 0
    skipped: bool = False        # 因资源过滤被跳过
    skip_reason: str = ""
    from_proxy: str = ""
    fetch_mode: str = "http"     # http | render | render-fallback
    rendered: bool = False
    blocked_resources: int = 0
    upgrade_reason: str = ""     # 从轻量升级到渲染的原因

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300 and not self.skipped


class HttpClient:
    """线程安全的 HTTP 客户端。内部持有连接池 + 信号量 + 代理游标。"""

    def __init__(self, config: Config = None, ua_profile: str = "pc"):
        self.cfg = config or DEFAULT_CONFIG
        self.ua = UAPool(profile=ua_profile, rotate=self.cfg.rotate_ua)
        self._lock = threading.Lock()
        self._proxy_idx = 0
        self._global_sem = threading.BoundedSemaphore(self.cfg.max_concurrency)
        self._host_sems: Dict[str, threading.BoundedSemaphore] = {}
        self._client: Optional[httpx.Client] = None
        self._proxy_clients: Dict[str, httpx.Client] = {}
        self._robots = None     # 由 engine 注入, 避免循环依赖

    # ---------- 生命周期 ----------
    def _build_client(self, proxy: str = "") -> httpx.Client:
        """构造一个 httpx.Client。

        代理必须在**构造时**传入。httpx >= 0.28 的 Client.get() 不再接受
        proxies / transport 参数, 以前那样传会直接 TypeError —— 而该 TypeError
        不匹配任何重试分支, 会被上层当作"抓取异常"静默降级, 代理从未生效
        且无人察觉。因此这里按代理串各建一个 client 并缓存。
        """
        kwargs: Dict[str, Any] = dict(
            timeout=httpx.Timeout(self.cfg.timeout,
                                  connect=self.cfg.connect_timeout),
            follow_redirects=True,
            verify=self.cfg.verify_ssl,
            limits=httpx.Limits(max_connections=self.cfg.max_concurrency * 2,
                                max_keepalive_connections=self.cfg.max_concurrency),
            http2=False,
        )
        if proxy:
            # socks5:// 需要 httpx[socks]; 缺失时 httpx 会抛明确异常,
            # 这里不做静默处理, 让配置错误可见
            kwargs["proxy"] = proxy
        return httpx.Client(**kwargs)

    @property
    def client(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = self._build_client()
            return self._client

    def _client_for(self, proxy: str) -> tuple:
        """取该代理对应的 client; 无代理时用默认 client。"""
        if not proxy:
            return self.client, False
        with self._lock:
            c = self._proxy_clients.get(proxy)
            if c is None:
                c = self._proxy_clients[proxy] = self._build_client(proxy)
            return c, True

    def close(self):
        with self._lock:
            clients = [self._client] + list(self._proxy_clients.values())
            self._client = None
            self._proxy_clients.clear()
        for c in clients:
            if c is None:
                continue
            try:
                c.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- 并发控制 ----------
    def _host_sem(self, host: str) -> threading.BoundedSemaphore:
        with self._lock:
            sem = self._host_sems.get(host)
            if sem is None:
                sem = threading.BoundedSemaphore(self.cfg.per_host_concurrency)
                self._host_sems[host] = sem
            return sem

    # ---------- 代理 ----------
    def _next_proxy(self) -> Optional[str]:
        if not self.cfg.proxies:
            return None
        with self._lock:
            p = self.cfg.proxies[self._proxy_idx % len(self.cfg.proxies)]
            self._proxy_idx += 1
            return p

    # ---------- 核心 ----------
    def get(self, url: str, *, headers: Dict[str, str] = None,
            params: Dict[str, Any] = None, timeout: float = None,
            retries: int = None, allow_media: bool = None,
            lang: str = "zh", source: str = "") -> FetchResponse:
        """带重试的 GET。抛出 UpstreamError / TimeoutError_ / BlockedError。"""
        retries = self.cfg.retries if retries is None else retries
        allow_media = self.cfg.fetch_media if allow_media is None else allow_media
        timeout = timeout or self.cfg.timeout
        host = urlparse(url).netloc
        last_err: Optional[Exception] = None
        attempt = 0

        while attempt <= retries:
            attempt += 1
            hdrs = dict(self.ua.headers(lang=lang))
            if headers:
                hdrs.update(headers)
            proxy = self._next_proxy()
            t0 = time.time()
            try:
                with self._global_sem, self._host_sem(host):
                    req_kwargs = dict(timeout=timeout, headers=hdrs, params=params)
                    cli, _ = self._client_for(proxy or "")
                    resp = cli.get(url, **req_kwargs)
                elapsed = int((time.time() - t0) * 1000)
                ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                fr = FetchResponse(
                    url=str(resp.url), status_code=resp.status_code,
                    headers=dict(resp.headers), content_type=ct,
                    elapsed_ms=elapsed, retries=attempt - 1,
                    from_proxy=proxy or "",
                )

                if not allow_media and self._is_media(ct, url):
                    fr.skipped = True
                    fr.skip_reason = f"资源过滤: {ct or 'unknown'}"
                    log.debug("跳过媒体资源 %s (%s)", url, ct)
                    return fr

                # 读取文本(限制大小, 防止超大页面打爆内存)
                raw = resp.content[: self.cfg.fetch_max_bytes]
                enc = resp.encoding or _detect_encoding(resp.headers, raw) or "utf-8"
                try:
                    fr.text = raw.decode(enc, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    fr.text = raw.decode("utf-8", errors="replace")

                # 反爬拦截识别
                if resp.status_code in (403, 429, 503) or self._looks_blocked(fr.text):
                    raise BlockedError(
                        f"疑似被拦截 status={resp.status_code}",
                        source=source or host, status_code=resp.status_code)

                if resp.status_code >= 400:
                    raise UpstreamError(
                        f"HTTP {resp.status_code}", source=source or host,
                        status_code=resp.status_code)

                return fr

            except BlockedError as e:
                last_err = e
                log.warning("被拦截 %s (第 %d 次): %s", url, attempt, e)
                if attempt > retries:
                    break
                self._sleep_backoff(attempt)
                continue
            except (httpx.TimeoutException, httpx.ConnectTimeout) as e:
                last_err = TimeoutError_(f"请求超时: {url} ({type(e).__name__})")
                log.warning("超时 %s (第 %d 次)", url, attempt)
                if attempt > retries:
                    break
                self._sleep_backoff(attempt)
                continue
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.ProxyError) as e:
                last_err = UpstreamError(f"网络错误: {type(e).__name__} {e}",
                                         source=source or host)
                if attempt > retries:
                    break
                self._sleep_backoff(attempt)
                continue
            except UpstreamError as e:
                # 4xx 不重试(除 429)
                if e.status_code and 400 <= e.status_code < 500 and e.status_code != 429:
                    raise
                last_err = e
                if attempt > retries:
                    break
                self._sleep_backoff(attempt)
                continue

        if isinstance(last_err, IntelliSearchErrorBase):
            raise last_err
        raise last_err or UpstreamError(f"请求失败: {url}", source=source)

    # ---------- 工具 ----------
    def _is_media(self, ct: str, url: str) -> bool:
        if ct and any(ct.startswith(p) for p in MEDIA_CT_PREFIXES):
            if ct == "application/pdf":
                return False   # PDF 属于文档, 走文件解析链路
            return True
        ext = url.rsplit(".", 1)[-1].lower() if "." in url else ""
        return ext in ("png", "jpg", "jpeg", "gif", "webp", "svg", "mp4", "webm",
                       "mp3", "woff", "woff2", "ttf", "otf", "css", "js", "ico")

    @staticmethod
    def _looks_blocked(text: str) -> bool:
        """是否为反爬拦截页。

        只在页面很小(典型的验证页)或同时命中 2 个以上强特征时才判定,
        避免把带验证码组件的正常大结果页误杀。
        """
        if not text or len(text) > 200_000:
            return False
        low = text[:4000].lower()
        hits = sum(1 for t in BLOCK_TOKENS if t in low)
        if hits >= 2:
            return True
        return hits >= 1 and len(text) < 2000

    def _sleep_backoff(self, attempt: int) -> None:
        base = self.cfg.backoff_base * (2 ** (attempt - 1))
        time.sleep(base + random.uniform(0, base * 0.3))

    def get_many(self, urls: Sequence[str], max_workers: int = None, **kw):
        """并发抓取。返回 [(url, FetchResponse | Exception)]。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        workers = min(max_workers or self.cfg.provider_max_concurrency, len(urls)) or 1
        out: List = []
        if not urls:
            return out
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(self.get, u, **kw): u for u in urls}
            for fut in as_completed(futs):
                u = futs[fut]
                try:
                    out.append((u, fut.result()))
                except Exception as e:      # noqa: BLE001
                    out.append((u, e))
        return out


def _detect_encoding(headers, raw: bytes) -> Optional[str]:
    import re as _re
    ct = headers.get("content-type", "")
    m = _re.search(r"charset=([\w-]+)", ct, _re.I)
    if m:
        return m.group(1)
    head = raw[:2048].decode("ascii", errors="ignore")
    m = _re.search(r'charset=["\']?([\w-]+)', head, _re.I)
    if m:
        return m.group(1)
    return None
