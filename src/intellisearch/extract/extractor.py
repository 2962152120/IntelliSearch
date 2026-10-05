"""网页正文抽取(自研, 零第三方依赖)。

算法(Readability 的轻量版):
1. 移除噪声容器: nav/header/footer/aside/form/广告位/评论区/侧栏;
2. 对每个候选块按「文本量 × 低链接密度 × 段落数 × 语义 class 加权」打分;
3. 取得分最高的块作为正文容器;
4. 按 <p> / 块级换行切分段落, 过滤过短噪声段, 去重段落;
5. 输出 clean_text + 段落级 segments(每段可回溯, 供引用标注)。

同时提取元信息: 标题 / 发布时间 / 作者 / 站点名 / 描述。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..extract.clean import (dedupe_paragraphs, detect_source_type, domain_of,
                             extract_date, normalize_text, truncate)
from ..extract.dom import Node, parse_html
from ..models import SourceType

POSITIVE_RE = re.compile(
    r"content|article|main|post|body|detail|text|entry|markdown|"
    r"rich_media|post-content|article-content|detail-content|txt", re.I)
NEGATIVE_RE = re.compile(
    r"comment|sidebar|nav|footer|header|related|recommend|ad|advert|"
    r"share|menu|widget|breadcrumb|pagination|copyright|copyright|login|"
    r"regist|subscribe|newsletter|toolbar|social|tags|crumb|promo", re.I)

NOISE_TAGS = {"nav", "header", "footer", "aside", "form", "button", "select",
              "noscript", "iframe", "svg", "script", "style"}

META_TIME_KEYS = [
    ("property", "article:published_time"),
    ("property", "og:pubdate"),
    ("name", "publishdate"),
    ("name", "publish_date"),
    ("name", "date"),
    ("name", "pubdate"),
    ("name", "DC.date.issued"),
    ("itemprop", "datePublished"),
    ("name", "created"),
    ("property", "article:modified_time"),
]
META_AUTHOR_KEYS = [
    ("name", "author"), ("property", "article:author"),
    ("itemprop", "author"), ("name", "byline"),
]


@dataclass
class ExtractedPage:
    url: str = ""
    final_url: str = ""
    title: str = ""
    clean_text: str = ""
    segments: List[Dict] = field(default_factory=list)
    summary: str = ""
    publish_time: Optional[str] = None
    author: str = ""
    site_name: str = ""
    description: str = ""
    source_type: SourceType = SourceType.UNKNOWN
    domain: str = ""
    lang: str = ""
    word_count: int = 0
    truncated: bool = False

    def to_dict(self):
        d = dict(self.__dict__)
        d["source_type"] = self.source_type.value
        return d


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def extract(html: str, url: str = "", max_chars: int = 4000,
            final_url: str = "") -> ExtractedPage:
    """从 HTML 中抽取正文与元信息。空输入返回空 ExtractedPage(不抛异常)。"""
    page = ExtractedPage(url=url, final_url=final_url or url,
                         domain=domain_of(final_url or url))
    if not html:
        return page
    root = parse_html(html)

    meta = _extract_meta(root)
    page.title = meta.get("title", "")
    page.publish_time = meta.get("publish_time")
    page.author = meta.get("author", "")
    page.site_name = meta.get("site_name", "")
    page.description = meta.get("description", "")
    page.lang = meta.get("lang", "")

    container, text = _extract_content(root)
    paras = _to_paragraphs(container, text)
    paras = dedupe_paragraphs(paras, min_len=18)
    page.segments = [{"index": i, "text": p} for i, p in enumerate(paras, 1)]
    full = "\n\n".join(paras)
    page.word_count = len(full)
    if max_chars and len(full) > max_chars:
        full = truncate(full, max_chars)
        page.truncated = True
    page.clean_text = full
    page.source_type = detect_source_type(final_url or url, page.title, full[:1500])
    if not page.publish_time:
        page.publish_time = extract_date(full[:500]) or extract_date(
            normalize_text(root.text_content())[:300])
    return page


# --------------------------------------------------------------------------
# 元信息
# --------------------------------------------------------------------------
def _extract_meta(root: Node) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}

    t = root.find("title")
    out["title"] = normalize_text(t.text_content()) if t else ""

    h = root.find("html")
    out["lang"] = h.get("lang", "") if h else ""

    metas = root.find_all("meta")
    props: Dict[str, str] = {}
    for m in metas:
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").lower()
        if key:
            props[key] = m.get("content", "")

    for attr, key in META_TIME_KEYS:
        v = props.get(key.lower())
        if v:
            d = extract_date(v)
            if d:
                out["publish_time"] = d
                break
    for attr, key in META_AUTHOR_KEYS:
        v = props.get(key.lower())
        if v:
            out["author"] = normalize_text(v)[:60]
            break

    out["site_name"] = props.get("og:site_name", "")
    out["description"] = normalize_text(
        props.get("description", "") or props.get("og:description", ""))[:500]

    # og:title 通常比 <title> 干净(后者常带站点后缀)
    og_title = props.get("og:title", "")
    if og_title and len(og_title) >= 4:
        out["title"] = normalize_text(og_title)
    return out


# --------------------------------------------------------------------------
# 正文定位
# --------------------------------------------------------------------------
def _score_node(n: Node) -> float:
    text = normalize_text(n.text_content())
    if len(text) < 120:
        return 0.0
    link_text = 0
    for a in n.find_all("a"):
        link_text += len(normalize_text(a.text_content()))
    density = 1.0 - (link_text / max(len(text), 1))
    density = max(density, 0.05)
    p_count = len(n.find_all("p"))
    mark = (n.attrs.get("class", "") + " " + n.attrs.get("id", ""))
    kw = 1.7 if POSITIVE_RE.search(mark) else 1.0
    neg = 0.25 if NEGATIVE_RE.search(mark) else 1.0
    if n.tag in NOISE_TAGS:
        neg *= 0.2
    # 段落平均长度: 正文段落普遍较长
    avg_p = (len(text) / max(p_count, 1))
    p_bonus = 1.0 + min(0.5, avg_p / 400.0)
    # 段落数因子**必须封顶**。不封顶时 <p> 极多的导航/广告容器会系统性
    # 战胜正文: 实测 W3Schools 的 #top-nav-bar(p=140) 得分 ×17.8,
    # 而真正的 #main(p=1) 只 ×1.12, 结果导航文案被当成正文抽走。
    p_factor = min(1 + p_count * 0.12, 2.0)
    return (len(text) ** 0.62) * density * p_factor * kw * neg * p_bonus


def _extract_content(root: Node):
    """返回 (正文容器节点, 兜底文本)。"""
    best: Optional[Node] = None
    best_score = 0.0
    for n in root.iter_nodes():
        if n.is_text or n.tag in ("html", "body", "root"):
            continue
        if n.tag in NOISE_TAGS:
            continue
        s = _score_node(n)
        if s > best_score:
            best_score, best = s, n
    body = root.find("body") or root
    fallback = normalize_text(body.text_content())
    if best is None or best_score <= 0:
        return body, fallback
    return best, normalize_text(best.text_content())


def _to_paragraphs(container: Node, fallback_text: str) -> List[str]:
    """按 <p> 优先切段; 没有 <p> 时按换行切。"""
    paras: List[str] = []
    ps = container.find_all("p") if container else []
    for p in ps:
        t = normalize_text(p.text_content())
        if len(t) >= 18 and not _looks_nav(t):
            paras.append(t)
    if len(paras) < 2:
        # 退化: 用容器内的块级节点文本
        cands: List[str] = []
        for n in (container.iter_nodes() if container else []):
            if n.is_text or n.tag not in ("div", "section", "article", "li",
                                          "blockquote", "pre", "td"):
                continue
            if n.find_all("div") or n.find_all("p"):
                continue     # 只取叶子块
            t = normalize_text(n.text_content())
            if len(t) >= 18 and not _looks_nav(t):
                cands.append(t)
        if len(cands) > len(paras):
            paras = cands
    if len(paras) < 2 and fallback_text:
        paras = [p.strip() for p in fallback_text.split("\n")
                 if len(p.strip()) >= 18 and not _looks_nav(p)]
    return paras


def _looks_nav(t: str) -> bool:
    """短且无句读、或纯链接列表 -> 判定为导航噪声。"""
    if len(t) > 60:
        return False
    if not re.search(r"[。！？，、；]", t):
        return True
    return False
