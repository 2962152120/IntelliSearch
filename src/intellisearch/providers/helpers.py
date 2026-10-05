"""HTML 结果页解析的公共工具。

各搜索引擎的 HTML 结构千差万别, 这里提供:
- 从任意块中稳健地抽取 {标题, 链接, 摘要, 日期}
- 处理高亮标签(strong/em/b)被拆到末尾导致的标题错位
- 搜索引擎跳转链接(如搜狗 /link?url=xxx)的解析
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse, unquote

from ..extract.clean import clean_snippet, extract_date
from ..extract.dom import Node, normalize_text
from ..models import SearchResult

HIGHLIGHT_TAGS = {"strong", "em", "b", "mark", "font", "span"}


def text_of(node: Node) -> str:
    return normalize_text(node.text_content())


def node_title(a: Node) -> str:
    """取链接标题。

    DOM 已按文档顺序保留文本与高亮标签(<em>/<strong>),
    因此这里直接取完整文本即可, 无需额外修复。
    """
    if a is None:
        return ""
    return normalize_text(a.text_content())


def pick_title_url(block: Node, base_url: str = "",
                   title_tags: Tuple[str, ...] = ("h2", "h3", "h4", "h1"),
                   title_classes: Tuple[str, ...] = (),
                   allow_redirect_links: bool = False) -> Tuple[str, str]:
    """从结果块中挑选最佳 (标题, 链接)。

    优先级: title_classes 指定容器 > 标题标签内链接 > 块内首个外链。
    allow_redirect_links=True 时接受搜索引擎的跳转链接(如 /link?url=xxx),
    后续由 resolve_redirects() 还原真实地址。
    """
    ok = _is_result_href if allow_redirect_links else _is_http
    candidates: List[Tuple[int, str, str]] = []

    for cls in title_classes:
        for n in block.find_all("div", cls):
            a = n.find("a")
            if a and ok(a.get("href")):
                candidates.append((3, node_title(a), a.get("href")))
        for n in block.find_all("h3", cls):
            a = n.find("a")
            if a and ok(a.get("href")):
                candidates.append((4, node_title(a), a.get("href")))

    for tag in title_tags:
        for n in block.find_all(tag):
            for a in n.find_all("a"):
                if ok(a.get("href")):
                    candidates.append((2, node_title(a), a.get("href")))
                    break

    for a in block.find_all("a"):
        href = a.get("href")
        if ok(href) and not _is_internal(href):
            candidates.append((1, node_title(a), href))

    if not candidates:
        return "", ""
    # 同分时取标题更长的(通常信息更完整)
    candidates.sort(key=lambda x: (x[0], len(x[1])), reverse=True)
    return candidates[0][1], _abs(candidates[0][2], base_url)


def pick_snippet(block: Node, exclude_text: str = "",
                 min_len: int = 20,
                 classes: Tuple[str, ...] = ()) -> str:
    """挑选最佳摘要: 优先指定 class, 否则取块内最长的段落文本。"""
    best = ""
    for cls in classes:
        for n in block.find_all("p", cls):
            t = text_of(n)
            if len(t) > len(best):
                best = t
    for n in block.find_all("p"):
        t = text_of(n)
        if len(t) > len(best):
            best = t
    if not best:
        for n in block.find_all("div"):
            if n.find_all("div"):     # 只要最内层(叶子)容器
                continue
            t = text_of(n)
            if len(t) > len(best):
                best = t
    if not best:
        best = text_of(block)
    if exclude_text:
        best = best.replace(exclude_text, " ").strip()
    s = clean_snippet(best)
    if not _usable_snippet(s):
        return ""      # 宁可留空, 也不要把站点归属/域名当成摘要
    return s


# 站点名(如 "哔哩哔哩" / "pc.zol.com.cn反馈")常被误当成摘要
_SITE_ONLY_RE = re.compile(
    r"^[\w\-.]+\.(?:com|cn|net|org|io|cc|tv|me|co|info|biz|dev)(?:\.cn)?"
    r"(?:反馈|官网)?$", re.I)


def _usable_snippet(s: str) -> bool:
    if not s or len(s) < 12:
        return False
    if _SITE_ONLY_RE.match(s.strip()):
        return False
    # 无标点且很短 -> 大概率是站点名/面包屑, 不是正文摘要
    if len(s) < 26 and not re.search(r"[。！？，、；,.!?]", s):
        return False
    return True


def pick_date(block: Node, snippet: str = "") -> Optional[str]:
    """尽力提取发布时间。"""
    for cls in ("cite-date", "date", "time", "publish", "news_dt", "b_factrow"):
        for n in block.find_all("span", cls):
            d = extract_date(text_of(n))
            if d:
                return d
        for n in block.find_all("div", cls):
            d = extract_date(text_of(n))
            if d:
                return d
    for tag in ("time",):
        n = block.find(tag)
        if n is not None:
            d = extract_date(n.get("datetime") or text_of(n))
            if d:
                return d
    d = extract_date(snippet or "")
    if d:
        return d
    return extract_date(text_of(block)[:300])


def _is_http(href: str) -> bool:
    return bool(href) and href.startswith(("http://", "https://"))


# 搜索引擎跳转链接: 搜狗 /link?url=..., 360 https://www.so.com/link?m=...
REDIRECT_RE = re.compile(r"^(?:/link\?|https?://[^/]+/link\?)", re.I)


def _is_result_href(href: str) -> bool:
    if not href:
        return False
    if _is_http(href) and not REDIRECT_RE.match(href):
        return True
    return bool(REDIRECT_RE.match(href))


def needs_resolve(url: str) -> bool:
    """该 URL 是否为需要还原的跳转链接。"""
    return bool(url) and (url.startswith("/link?") or bool(REDIRECT_RE.match(url)))


def _is_internal(href: str) -> bool:
    """搜索引擎自身的站内链接(如 /search?...)不算结果链接。"""
    low = href.lower()
    if low.startswith("/link?") or "so.com/link?" in low:
        return False
    return low.startswith("/") or "javascript:" in low or "#" in low[:6]


def _abs(href: str, base: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/") and base:
        return urljoin(base, href)
    return href


def unwrap_redirect(href: str) -> str:
    """尝试从跳转链接中直接还原真实 URL(不发起请求)。

    例: /link?url=https%3A%2F%2Fexample.com -> https://example.com
    """
    if not href:
        return ""
    m = re.search(r"[?&]url=([^&]+)", href)
    if m:
        v = unquote(m.group(1))
        if v.startswith("http"):
            return v
    return ""


def blocks_by_regex(html: str, pattern: str) -> List[str]:
    """按正则切出结果块(用于 DOM 解析不稳定时的兜底)。"""
    return re.findall(pattern, html, re.S | re.I)


# --------------------------------------------------------------------------
# 跳转链接还原
# --------------------------------------------------------------------------
_JS_REDIRECT_RES = (
    re.compile(r"""(?:window\.)?location\s*\.\s*(?:replace|href\s*=)\s*\(?\s*["'](https?://[^"']+)""", re.I),
    re.compile(r"""window\.open\s*\(\s*["'](https?://[^"']+)""", re.I),
    re.compile(r"""<meta[^>]+http-equiv=["']refresh["'][^>]+url=["']?([^"'>\s]+)""", re.I),
)


def extract_redirect_target(html: str) -> str:
    """从跳转中间页里抽取真实目标(用于 JS / meta refresh 跳转)。"""
    if not html:
        return ""
    for rx in _JS_REDIRECT_RES:
        m = rx.search(html)
        if m:
            u = m.group(1).strip()
            if u.startswith("http"):
                return u
    return ""


def resolve_redirects(results: List[SearchResult], ctx,
                      max_workers: int = 6, timeout: float = 8.0) -> List[SearchResult]:
    """把搜索引擎跳转链接批量还原为真实 URL。

    无法还原的结果保留原链接(仍可展示), 不丢弃 —— 保证部分可用。
    """
    targets = [r for r in results if needs_resolve(r.url)]
    if not targets:
        return results

    def work(r: SearchResult):
        try:
            resp = ctx.http.get(_abs(r.url, "https://www.sogou.com"),
                                timeout=timeout, retries=1,
                                source="redirect")
            real = ""
            if resp.status_code == 200 and resp.text:
                real = extract_redirect_target(resp.text)
            final = str(resp.url or "")
            # 跟随 3xx 后的最终地址若不是跳转页本身, 优先用它
            if not real and final and not needs_resolve(final):
                real = final
            if real:
                r.url = real
                r.domain = ""
                r.__post_init__()
        except Exception:      # noqa: BLE001 - 单条失败不影响其它
            pass
        return r

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets)) or 1) as pool:
        list(pool.map(work, targets))
    return results
