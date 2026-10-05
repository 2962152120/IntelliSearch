"""核心数据结构定义。

设计约束(来自需求):
1. 结构化输出至少包含 title / url / snippet / publish_time;
2. 结果必须能直接作为大模型上下文引用 -> 内置 to_context();
3. 无结果或检索失败时返回明确状态 -> Status + ProviderReport。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------
class Status(str, Enum):
    """整体检索状态。调用方只需判断这一个字段。"""

    OK = "ok"                    # 至少一个源成功且有结果
    PARTIAL = "partial"          # 部分源失败, 但仍有结果可用
    NO_RESULTS = "no_results"    # 源都通了, 但确实没有匹配结果(正常业务状态)
    ERROR = "error"              # 全部源失败 / 参数非法 / 被限流
    BLOCKED = "blocked"          # 命中安全策略或 robots 禁止


class SourceType(str, Enum):
    """页面类型标签, 供排序权重与大模型理解使用。"""

    DOC = "doc"              # 技术文档 / 官方文档
    WIKI = "wiki"            # 百科
    NEWS = "news"            # 新闻
    FORUM = "forum"          # 论坛 / 社区
    PRODUCT = "product"      # 商品详情
    BLOG = "blog"            # 博客 / 自媒体
    ACADEMIC = "academic"    # 论文
    CODE = "code"            # 代码仓库
    SOCIAL = "social"        # 社交媒体
    PDF = "pdf"              # PDF 文档
    UNKNOWN = "unknown"


class Freshness(str, Enum):
    DAY = "1d"
    WEEK = "1w"
    MONTH = "1m"
    YEAR = "1y"
    ANY = "any"


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
@dataclass
class QueryOptions:
    """检索请求的可选控制参数。"""

    top_k: int = 10                       # 返回条数上限
    freshness: Freshness = Freshness.ANY  # 时效性过滤
    time_from: Optional[datetime] = None  # 自定义时间下界
    time_to: Optional[datetime] = None    # 自定义时间上界
    site: Optional[str] = None            # 站点过滤: 只搜该域名
    exclude_sites: List[str] = field(default_factory=list)
    providers: Optional[List[str]] = None  # 指定检索源; None=按策略自动选
    lang: str = "zh"                      # zh / en
    fetch_content: bool = False           # 是否抓取正文
    max_content_chars: int = 4000         # 单页正文最大字符数
    summarize: bool = True                # 是否生成摘要(截断式摘要, 非 LLM)
    context_window: Optional[int] = None  # 输出压缩目标字符数
    session_id: Optional[str] = None      # 会话维度上下文记忆
    timeout: float = 15.0                 # 单请求超时(秒)
    retries: int = 2                      # 失败退避重试次数
    use_cache: bool = True
    cache_ttl: int = 1800                 # 缓存秒数
    vertical: Optional[str] = None        # 垂直场景: tech / general

    def __post_init__(self):
        if self.top_k < 1:
            self.top_k = 1
        if self.top_k > 50:
            self.top_k = 50

    def cache_key(self, raw_query: str) -> str:
        """生成缓存键。不含 session_id / timeout 等不影响结果语义的字段。"""
        payload = "|".join(
            [
                raw_query.strip().lower(),
                str(self.top_k),
                self.freshness.value,
                str(self.site or ""),
                ",".join(sorted(self.exclude_sites)),
                str(self.time_from.isoformat() if self.time_from else ""),
                str(self.time_to.isoformat() if self.time_to else ""),
                self.lang,
                "1" if self.fetch_content else "0",
                str(self.max_content_chars),
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()


@dataclass
class ParsedQuery:
    """Query 解析与改写结果。"""

    raw: str                                  # 原始自然语言问题
    effective: str                            # 实际下发给搜索引擎的 query
    terms: List[str] = field(default_factory=list)   # 分词后的核心词
    must: List[str] = field(default_factory=list)    # AND
    should: List[str] = field(default_factory=list)  # OR(同义扩展)
    not_: List[str] = field(default_factory=list)    # NOT
    site: Optional[str] = None
    expanded: List[str] = field(default_factory=list)  # 改写出的多个子查询
    rewritten: bool = False

    def to_dict(self):
        return asdict(self)


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------
_TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "share_token", "ref", "referrer", "share_from",
    "bd_source", "channel", "src", "source", "sid", "gid", "ei", "ved",
    "usg", "sa", "biw", "bih", "cd", "gs_l", "oq", "gs_lcp",
}


def canonical_url(url: str) -> str:
    """URL 归一化: 去协议/www/末尾斜杠/fragment/跟踪参数。用于去重。"""
    if not url:
        return ""
    try:
        p = urlparse(url)
    except Exception:
        return url
    if p.scheme not in ("http", "https"):
        return url
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/+$", "", p.path) or "/"
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
         if k.lower() not in _TRACKING_KEYS]
    q.sort()
    return urlunparse((p.scheme, netloc, path, "", urlencode(q), ""))


def url_fingerprint(url: str) -> str:
    return hashlib.md5(canonical_url(url).encode("utf-8")).hexdigest()


@dataclass
class SearchResult:
    """单条检索结果 —— 需求要求的最小字段集 + AI 适配扩展字段。"""

    title: str
    url: str
    snippet: str = ""                     # 摘要(搜索源摘要或抽取摘要)
    publish_time: Optional[str] = None    # ISO8601 字符串, 无则 None
    source: str = ""                      # 来自哪个检索源(如 bing_rss)
    source_type: SourceType = SourceType.UNKNOWN
    domain: str = ""
    relevance_score: float = 0.0          # 0~1 综合得分(用于排序)
    credibility: float = 0.5              # 0~1 来源可信度
    clean_text: str = ""                  # 抓取并清洗后的正文(可选)
    segments: List[Dict[str, Any]] = field(default_factory=list)  # 段落级引用
    fetched_at: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.domain and self.url:
            try:
                self.domain = urlparse(self.url).netloc.lower()
            except Exception:
                self.domain = ""
        if self.domain.startswith("www."):
            self.domain = self.domain[4:]
        if self.publish_time and isinstance(self.publish_time, datetime):
            self.publish_time = self.publish_time.isoformat()

    @property
    def fingerprint(self) -> str:
        return url_fingerprint(self.url)

    @property
    def publish_dt(self) -> Optional[datetime]:
        return parse_time(self.publish_time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["source_type"] = self.source_type.value
        return d

    def to_ai_dict(self, max_snippet: Optional[int] = None) -> Dict[str, Any]:
        """大模型友好的精简结构。"""
        snip = self.snippet or ""
        if max_snippet and len(snip) > max_snippet:
            snip = snip[:max_snippet] + "…"
        return {
            "title": self.title,
            "url": self.url,
            "publish_time": self.publish_time,
            "source_type": self.source_type.value,
            "snippet": snip,
            "relevance_score": round(self.relevance_score, 4),
            "credibility": round(self.credibility, 4),
        }


@dataclass
class ProviderReport:
    """单个检索源的执行报告 —— 失败可见化。"""

    name: str
    ok: bool
    status: str = ""            # ok / timeout / blocked / error / empty / skipped
    count: int = 0
    elapsed_ms: int = 0
    error: str = ""
    retries: int = 0

    def to_dict(self):
        return asdict(self)


@dataclass
class SearchResponse:
    """统一返回结构。永远返回本对象, 不抛异常。"""

    query: str
    status: Status = Status.OK
    results: List[SearchResult] = field(default_factory=list)
    parsed_query: Optional[ParsedQuery] = None
    providers: List[ProviderReport] = field(default_factory=list)
    total_found: int = 0
    elapsed_ms: int = 0
    cached: bool = False
    message: str = ""
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def ok(self) -> bool:
        return self.status in (Status.OK, Status.PARTIAL) and bool(self.results)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "status": self.status.value,
            "ok": self.ok,
            "message": self.message,
            "elapsed_ms": self.elapsed_ms,
            "cached": self.cached,
            "total_found": self.total_found,
            "returned": len(self.results),
            "parsed_query": self.parsed_query.to_dict() if self.parsed_query else None,
            "providers": [p.to_dict() for p in self.providers],
            "conflicts": self.conflicts,
            "results": [r.to_dict() for r in self.results],
            "created_at": self.created_at,
        }

    def to_json(self, ensure_ascii: bool = False, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=ensure_ascii, indent=indent)

    def to_ai_dict(self, max_chars: Optional[int] = None,
                   max_snippet: int = 300) -> Dict[str, Any]:
        """给大模型的最小上下文结构。"""
        payload = {
            "query": self.query,
            "status": self.status.value,
            "results": [r.to_ai_dict(max_snippet) for r in self.results],
        }
        if self.conflicts:
            payload["conflicts"] = self.conflicts
        if max_chars:
            s = json.dumps(payload, ensure_ascii=False)
            if len(s) > max_chars:
                # 逐条裁剪, 保证条数优先于长度
                while payload["results"] and len(json.dumps(payload, ensure_ascii=False)) > max_chars:
                    payload["results"].pop()
                payload["truncated"] = True
        return payload

    def to_context(self, max_chars: int = 6000, with_content: bool = False) -> str:
        """渲染为可直接塞进 prompt 的带编号引用文本。"""
        lines = [f"# 检索结果（query: {self.query}，状态: {self.status.value}）"]
        if not self.results:
            lines.append(self.message or "未检索到相关结果。")
            return "\n".join(lines)
        used = len(lines[0])
        for i, r in enumerate(self.results, 1):
            head = f"\n[{i}] {r.title}"
            meta_parts = [r.domain]
            if r.publish_time:
                meta_parts.append(r.publish_time[:10])
            meta_parts.append(r.source_type.value)
            meta = f"    (来源: {' | '.join(meta_parts)})"
            body = r.clean_text if (with_content and r.clean_text) else r.snippet
            body = re.sub(r"\s+\n", "\n", (body or "")).strip()
            block = f"{head}\n{meta}\n    {r.url}\n    {body}"
            if used + len(block) > max_chars:
                keep = max_chars - used
                if keep > 120:
                    block = block[:keep] + " …(截断)"
                else:
                    break
            lines.append(block)
            used += len(block)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 时间解析
# --------------------------------------------------------------------------
_CN_WEEKDAYS = "周|星期|礼拜"
_TIME_PATTERNS = [
    (r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?[ T]?(\d{1,2})?:?(\d{2})?:?(\d{2})?", None),
]


def parse_time(value: Any) -> Optional[datetime]:
    """尽力把各种时间表示解析为 datetime。失败返回 None(不抛异常)。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None

    # Bing RSS 中文格式: "周五, 02 10月 2026 20:31:00 GMT"
    m = re.search(
        r"(?:周|星期|礼拜)[一二三四五六日天]\s*[,，]?\s*(\d{1,2})\s*(\d{1,2})月\s*(\d{4})\s*"
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        d, mo, y, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        ss = int(m.group(6) or 0)
        try:
            return datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
        except Exception:
            return None

    # 英文 RFC822: "Fri, 02 Oct 2026 20:31:00 GMT"
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    # ISO8601
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    # 2026-10-02 / 2026/10/2 / 2026年10月2日
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=timezone.utc)
        except Exception:
            return None

    # 相对时间: 3小时前 / 2天前 / 昨天
    m = re.search(r"(\d+)\s*(分钟|小时|天|周|个月|月|年)前", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {"分钟": 60, "小时": 3600, "天": 86400, "周": 604800,
                 "个月": 2592000, "月": 2592000, "年": 31536000}[unit]
        return datetime.fromtimestamp(time.time() - n * delta, tz=timezone.utc)
    if "昨天" in s:
        return datetime.fromtimestamp(time.time() - 86400, tz=timezone.utc)
    if "今天" in s or "小时前" in s:
        return datetime.fromtimestamp(time.time(), tz=timezone.utc)
    return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
