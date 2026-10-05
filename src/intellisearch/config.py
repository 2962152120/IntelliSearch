"""全局配置。优先级: 显式传参 > 环境变量 > 默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_list(key: str, default: str = "") -> List[str]:
    raw = os.getenv(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Config:
    # ---- 网络 ----
    timeout: float = _env_float("IS_TIMEOUT", 15.0)
    connect_timeout: float = _env_float("IS_CONNECT_TIMEOUT", 5.0)
    retries: int = _env_int("IS_RETRIES", 2)
    backoff_base: float = _env_float("IS_BACKOFF_BASE", 0.6)   # 指数退避基数
    max_concurrency: int = _env_int("IS_MAX_CONCURRENCY", 8)    # 全局并发连接上限
    per_host_concurrency: int = _env_int("IS_PER_HOST_CONCURRENCY", 2)
    verify_ssl: bool = _env_bool("IS_VERIFY_SSL", True)
    proxies: List[str] = field(default_factory=lambda: _env_list("IS_PROXIES"))
    rotate_ua: bool = _env_bool("IS_ROTATE_UA", True)

    # ---- 检索源 ----
    providers: List[str] = field(default_factory=lambda: _env_list(
        "IS_PROVIDERS", "bing_rss,bing_html,sogou,so360"))
    provider_max_concurrency: int = _env_int("IS_PROVIDER_CONCURRENCY", 6)

    # 需要 API Key 的商业源(配置了 key 才会启用)
    tavily_key: str = os.getenv("TAVILY_API_KEY", "")
    serpapi_key: str = os.getenv("SERPAPI_API_KEY", "")
    brave_key: str = os.getenv("BRAVE_API_KEY", "")

    # ---- 缓存 ----
    cache_enabled: bool = _env_bool("IS_CACHE", True)
    cache_ttl: int = _env_int("IS_CACHE_TTL", 1800)
    cache_path: str = os.getenv(
        "IS_CACHE_PATH",
        os.path.join(os.path.expanduser("~"), ".intellisearch", "cache.sqlite3"))
    cache_max_entries: int = _env_int("IS_CACHE_MAX_ENTRIES", 5000)

    # ---- 限流 ----
    rate_limit_enabled: bool = _env_bool("IS_RATE_LIMIT", True)
    global_qps: float = _env_float("IS_GLOBAL_QPS", 5.0)
    global_burst: int = _env_int("IS_GLOBAL_BURST", 10)
    session_qps: float = _env_float("IS_SESSION_QPS", 1.0)
    session_burst: int = _env_int("IS_SESSION_BURST", 3)

    # ---- 抓取 ----
    fetch_max_bytes: int = _env_int("IS_FETCH_MAX_BYTES", 3_000_000)
    fetch_media: bool = _env_bool("IS_FETCH_MEDIA", False)   # 默认不下载图片/视频/字体/CSS
    respect_robots: bool = _env_bool("IS_RESPECT_ROBOTS", True)

    # ---- 浏览器渲染(内置 Chromium 内核, 无需任何第三方 API Key) ----
    # 默认开启, 但仅在"轻量 HTTP 拿不到实质内容"时才真正启动浏览器,
    # 因此静态页场景零额外开销。找不到可用内核时自动退回轻量抓取。
    render_enabled: bool = _env_bool("IS_RENDER", True)
    render_headless: bool = _env_bool("IS_RENDER_HEADLESS", True)   # False=有头(调试)
    render_timeout: float = _env_float("IS_RENDER_TIMEOUT", 25.0)    # 单页渲染总超时
    render_wait_until: str = os.getenv("IS_RENDER_WAIT_UNTIL", "domcontentloaded")
    render_wait_selector: str = os.getenv("IS_RENDER_WAIT_SELECTOR", "")
    render_settle_ms: int = _env_int("IS_RENDER_SETTLE_MS", 0)      # 网络静默等待
    render_scroll: bool = _env_bool("IS_RENDER_SCROLL", False)      # 滚动触发懒加载
    render_block_resources: bool = _env_bool("IS_RENDER_BLOCK_RES", True)
    render_max_pages: int = _env_int("IS_RENDER_MAX_PAGES", 4)      # 并发页签上限
    browser_path: str = os.getenv("IS_BROWSER_PATH", "")            # 手动指定内核
    # 兼容旧字段
    headless_enabled: bool = _env_bool("IS_HEADLESS", True)
    chrome_path: str = os.getenv("IS_CHROME_PATH", "")

    # ---- 排序权重 ----
    w_relevance: float = _env_float("IS_W_RELEVANCE", 1.0)
    w_freshness: float = _env_float("IS_W_FRESHNESS", 0.35)
    w_credibility: float = _env_float("IS_W_CREDIBILITY", 0.3)
    w_length: float = _env_float("IS_W_LENGTH", 0.08)

    # ---- 安全 ----
    blacklist_terms: List[str] = field(default_factory=lambda: _env_list(
        "IS_BLACKLIST", ""))
    block_suspicious: bool = _env_bool("IS_BLOCK_SUSPICIOUS", True)

    # ---- 日志 ----
    log_level: str = os.getenv("IS_LOG_LEVEL", "INFO")
    audit_log: bool = _env_bool("IS_AUDIT_LOG", True)
    audit_path: str = os.getenv(
        "IS_AUDIT_PATH",
        os.path.join(os.path.expanduser("~"), ".intellisearch", "audit.log"))

    # ---- 默认域名可信度(0~1), 越高越可信 ----
    credibility_overrides: Dict[str, float] = field(default_factory=lambda: {
        # 官方/权威优先
        "amd.com": 0.95, "intel.com": 0.95, "nvidia.com": 0.95,
        "docs.python.org": 0.95, "developer.mozilla.org": 0.95,
        "github.com": 0.9, "stackoverflow.com": 0.88, "wikiwand.com": 0.7,
        "wikipedia.org": 0.78, "zh.wikipedia.org": 0.75, "baike.baidu.com": 0.6,
        "zhihu.com": 0.55, "csdn.net": 0.5, "jianshu.com": 0.45,
        "toutiao.com": 0.4, "163.com": 0.5, "sina.com.cn": 0.55,
        "smzdm.com": 0.5, "bilibili.com": 0.55, "weibo.com": 0.4,
        "cnblogs.com": 0.6, "juejin.cn": 0.6, "segmentfault.com": 0.6,
    })


DEFAULT_CONFIG = Config()
