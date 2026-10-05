"""文本清洗与降噪工具。

被 provider(结果摘要) 与 extractor(正文) 共用。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Tuple
from urllib.parse import urlparse

from ..models import SourceType, parse_time
from .dom import normalize_text

# 摘要中常见的日期前缀: "2026年9月24日 · ..." / "2026-09-24 - ..."
_DATE_PREFIX_RE = re.compile(
    r"^\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)\s*[·•\-–—|,，。\s]{1,3}\s*"
)
_DATE_INLINE_RE = re.compile(
    r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)")

# 广告 / 采集站 / 垃圾内容特征
_SPAM_PATTERNS = re.compile(
    r"(?i)(广告|推广| sponsored |buy now|点击购买|立即下载app|"
    r"本站所有内容均来自互联网|免责声明：本站|Copyright © .* All rights reserved|"
    r"如侵权请联系我们删除)")

# 站点类型判定规则: (URL 特征, 标题特征) -> SourceType
_TYPE_RULES = [
    (r"github\.com|gitlab\.com|gitee\.com", r"", SourceType.CODE),
    (r"\.pdf($|\?)", r"", SourceType.PDF),
    (r"arxiv\.org|ieee\.org|acm\.org|sciencedirect|springer|cnki\.net|"
     r"scholar\.google|pubmed|researchgate", r"", SourceType.ACADEMIC),
    (r"wikipedia\.org|baike\.baidu|wiki\.|baike\.", r"百科|百科|wiki", SourceType.WIKI),
    (r"docs\.|developer\.|documentation|readthedocs|\.dev/|api\.|"
     r"developer\.mozilla|learn\.microsoft|help\.|manual|support\.",
     r"文档|教程|documentation|api|手册", SourceType.DOC),
    (r"news\.|/\d{4}[-/]\d{2}[-/]\d{2}/|sina\.com\.cn|163\.com|qq\.com/news|"
     r"thepaper\.cn|chinanews|reuters|bbc\.com|cnn\.com",
     r"新闻|报道|快讯", SourceType.NEWS),
    (r"zhihu\.com|stackoverflow|segmentfault|cnblogs|juejin|csdn|"
     r"v2ex|reddit|quora|tieba|cnblogs|51cto|bbs\.|forum|discourse",
     r"问答|怎么|如何|论坛|讨论", SourceType.FORUM),
    (r"jd\.com|taobao\.com|tmall\.com|amazon\.|smzdm|item\.|product|/dp/",
     r"价格|多少钱|参数|规格|购买", SourceType.PRODUCT),
    (r"weibo\.com|twitter\.com|x\.com|xiaohongshu|douyin|facebook|instagram",
     r"", SourceType.SOCIAL),
    (r"blog\.|medium\.com|jianshu|wordpress|blogspot|csdn\.net|51cto\.com",
     r"", SourceType.BLOG),
]


def strip_date_prefix(text: str) -> Tuple[Optional[str], str]:
    """剥离摘要开头的日期, 返回 (日期串, 去日期后的摘要)。

    例: "2026年9月24日 · 铭字的读音是..." -> ("2026年9月24日", "铭字的读音是...")
    """
    if not text:
        return None, ""
    m = _DATE_PREFIX_RE.match(text)
    if m:
        raw = m.group(1)
        rest = text[m.end():]
        dt = parse_time(raw)
        return (dt.isoformat() if dt else raw), rest.strip()
    return None, text.strip()


def extract_date(text: str) -> Optional[str]:
    """从任意文本中尽力提取一个日期(优先开头)。"""
    if not text:
        return None
    d, _ = strip_date_prefix(text)
    if d:
        return d
    m = _DATE_INLINE_RE.search(text)
    if m:
        dt = parse_time(m.group(1))
        return dt.isoformat() if dt else None
    for pat in (r"(\d+)\s*(?:分钟|小时|天|周|个月|月|年)前", r"昨天", r"前天"):
        m = re.search(pat, text)
        if m:
            dt = parse_time(m.group(0))
            if dt:
                return dt.isoformat()
    return None


def clean_snippet(text: str, max_len: int = 500) -> str:
    """摘要降噪: 去日期前缀、合并空白、去重复片段、截断。"""
    if not text:
        return ""
    _, s = strip_date_prefix(text)
    s = normalize_text(s)
    s = re.sub(r"\.{3,}|…{2,}", "…", s)
    s = re.sub(r"(。)\1+", "。", s)
    # 去除重复的相邻句子
    parts = re.split(r"(?<=[。！？;；])", s)
    seen, out = set(), []
    for p in parts:
        key = p.strip()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    s = "".join(out).strip()
    # 剥离尾部的日期残留(如 "… 2025年9月27日 - ")
    s = re.sub(r"[\s·•\-–—|,，。]{1,3}\s*"
               r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?"
               r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"
               r"[\s·•\-–—|,，。]{0,3}\s*$", "", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip() + "…"
    return s


def dedupe_paragraphs(paras: list, min_len: int = 12) -> list:
    """去除正文中的重复段落。"""
    seen, out = set(), []
    for p in paras:
        p = (p or "").strip()
        if len(p) < min_len:
            continue
        key = re.sub(r"\s+", "", p)[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def is_spam(text: str) -> bool:
    """粗判广告/采集站内容。"""
    if not text:
        return False
    return bool(_SPAM_PATTERNS.search(text[:2000]))


def detect_source_type(url: str = "", title: str = "",
                       text: str = "") -> SourceType:
    """页面类型识别: URL 规则优先, 其次标题/正文规则。"""
    url_l = (url or "").lower()
    title_l = (title or "").lower()
    # 两轮: 第一轮要求 URL 与标题同时命中(强信号), 第二轮只看 URL
    for strict in (True, False):
        for url_pat, title_pat, stype in _TYPE_RULES:
            if not re.search(url_pat, url_l):
                continue
            if strict and title_pat and not re.search(title_pat, title_l):
                continue
            if not strict and title_pat and not re.search(title_pat, title_l):
                # 弱信号: URL 命中但标题没命中, 仍然采用(URL 更可靠)
                pass
            return stype
    return SourceType.UNKNOWN


def truncate(text: str, max_chars: int, tail_note: str = " …(截断)") -> str:
    """按字符数截断, 尽量在句读处断开。"""
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text or ""
    seg = text[:max_chars]
    for p in ("。", "！", "？", "\n", "；", "，", " "):
        idx = seg.rfind(p)
        if idx > max_chars * 0.6:
            return seg[: idx + 1].rstrip() + tail_note
    return seg.rstrip() + tail_note


def summarize(text: str, max_chars: int = 300) -> str:
    """抽取式摘要(不用 LLM): 取正文前部 + 关键句。

    策略: 优先取开头(文章通常在首段给出结论), 再补充包含数字/结论词的高信息句。
    """
    text = normalize_text(text or "")
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    sentences = [s.strip() for s in re.split(r"(?<=[。！？])", text) if s.strip()]
    if not sentences:
        return truncate(text, max_chars)
    head = sentences[0]
    picked = [head]
    used = len(head)
    # 高信息句: 含数字/单位/结论词
    key_re = re.compile(r"\d|%|TOPS|GHz|GB|TB|元|美元|发布|表示|支持|采用|参数|规格|"
                        r"结论|因此|综上|官方|评测|测试")
    for s in sentences[1:]:
        if used >= max_chars:
            break
        if key_re.search(s) and len(s) >= 10:
            if used + len(s) + 1 > max_chars:
                break
            picked.append(s)
            used += len(s) + 1
    out = "".join(picked)
    return out if len(out) >= max_chars * 0.5 else truncate(text, max_chars)


def domain_of(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return d[4:] if d.startswith("www.") else d


def root_domain(url_or_domain: str) -> str:
    """取注册域(去掉子域), 用于站点级过滤。"""
    d = domain_of(url_or_domain) if "://" in url_or_domain else url_or_domain.lower()
    parts = d.split(".")
    if len(parts) <= 2:
        return d
    # 常见二级后缀
    if len(parts) >= 3 and parts[-2] in ("com", "co", "org", "net", "gov", "edu",
                                         "ac", "com"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
