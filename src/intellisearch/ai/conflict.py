"""多源信息冲突检测(需求四.3)。

思路:
1. 从结果文本中抽取「关键词 + 数值 + 单位」形式的事实;
2. 只保留与本次查询核心词相关的事实(避免全文数字噪声);
3. 同一 (关键词, 单位) 下, 若不同来源给出差异超过阈值的数值, 标记为冲突,
   并附上各来源的说法, 让大模型知道"不同来源说法不一样"。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Sequence, Tuple

from ..models import SearchResult

UNITS = (r"TOPS|TFLOPS|GHz|MHz|kHz|nm|W|mW|V|A|GB|TB|MB|KB|Gbps|Mbps|"
         r"元|美元|万|亿|%|港元|人民币|块|fps|FPS|nit|cd|mAh|Wh|mm|cm|inch")

# 关键词与数值之间允许的字符(如 "NPU 算力 50" / "算力: 50" / "NPU算力达50")
FACT_RE = re.compile(
    rf"([A-Za-z\u4e00-\u9fff]{{2,12}}?)[^\d\n]{{0,14}}?"
    rf"(\d+(?:\.\d+)?)\s*({UNITS})(?![A-Za-z])", re.I)

# 与查询主题无关的泛用词, 不参与冲突判定
GENERIC_KEYS = {
    "价格", "元", "时间", "日期", "数量", "次数", "人数", "天数", "月", "年",
    "版本", "编号", "序号", "分数", "评分", "评论", "阅读", "点赞",
}


def _norm_key(k: str) -> str:
    k = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", k or "")
    return k.lower()


def _full_key(text: str, m: re.Match) -> str:
    """取完整关键词(修复惰性匹配造成的词尾截断)。

    FACT_RE 的关键词是惰性的(为了让 "算力 50" / "算力: 50" 都能匹配), 代价是
    ASCII 词会被 gap 吞掉尾部: "PRICE 2999 元" 抽成 "pr"、"NPU 算力 50 TOPS"
    抽成 "np"。不同词因此塌缩成同一前缀(price/product 都变 "pr"), 在
    detect_conflicts 里就会制造**假冲突**, 冲突消息里的关键词也成了残片。
    这里把关键词两侧紧邻的 ASCII 字母/数字并回来(不跨空格/中文, 总长 <=12)。
    """
    start, end = m.start(1), m.end(1)
    n = len(text)
    while end < n and end - start < 12 and text[end].isascii() and \
            text[end].isalnum():
        end += 1
    while start > 0 and end - start < 12 and text[start - 1].isascii() and \
            text[start - 1].isalnum():
        start -= 1
    return _norm_key(text[start:end])


def extract_facts(text: str, relevant_terms: Sequence[str] = ()) -> List[Tuple[str, float, str]]:
    """抽取 (关键词, 数值, 单位) 三元组。

    relevant_terms 非空时, 只保留关键词与查询词相关的事实。
    """
    if not text:
        return []
    text = text[:4000]
    rel = {_norm_key(t) for t in relevant_terms if t}
    out: List[Tuple[str, float, str]] = []
    for m in FACT_RE.finditer(text):
        key, val, unit = _full_key(text, m), float(m.group(2)), m.group(3).upper()
        if not key or key in GENERIC_KEYS:
            continue
        if rel:
            hit = key in rel or any(k in key or key in k for k in rel if len(k) >= 2)
            if not hit:
                continue
        if val <= 0 or val > 1e9:
            continue
        out.append((key, val, unit))
    return out


def detect_conflicts(results: Sequence[SearchResult],
                     terms: Sequence[str] = (),
                     rel_tolerance: float = 0.02,
                     min_sources: int = 2) -> List[Dict]:
    """检测多个来源之间的信息冲突。

    返回形如::

        [{"keyword": "npu算力", "unit": "TOPS",
          "claims": [{"value": 50.0, "source": "a.com", "url": "...", "text": "..."}],
          "conflict": True, "message": "关于「npu算力」，不同来源说法存在差异：50.0 TOPS / 48.0 TOPS"}]
    """
    buckets: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
    for r in results:
        text = f"{r.title}\n{r.snippet}\n{(r.clean_text or '')[:2500]}"
        seen = set()
        for key, val, unit in extract_facts(text, terms):
            k = (key, unit)
            if (k, val) in seen:
                continue
            seen.add((k, val))
            buckets[k].append({
                "value": val,
                "unit": unit,
                "source": r.domain or r.source,
                "url": r.url,
                "title": r.title[:80],
            })

    conflicts: List[Dict] = []
    for (key, unit), claims in buckets.items():
        sources = {c["source"] for c in claims}
        if len(sources) < min_sources and len(claims) < 2:
            continue
        values = sorted({c["value"] for c in claims})
        if len(values) < 2:
            continue
        lo, hi = values[0], values[-1]
        # 相对差异超过阈值才判定为冲突(容忍单位换算/四舍五入)
        diff = (hi - lo) / max(abs(hi), abs(lo), 1e-9)
        if diff <= rel_tolerance:
            continue
        # 每个来源只保留一条代表性说法
        uniq: List[Dict] = []
        seen_src = set()
        for c in claims:
            if c["source"] in seen_src:
                continue
            seen_src.add(c["source"])
            uniq.append(c)
        vs = " / ".join(f"{c['value']:g} {unit}" for c in uniq[:4])
        conflicts.append({
            "keyword": key,
            "unit": unit,
            "conflict": True,
            "claims": uniq[:6],
            "message": f"【信息存在差异】关于「{key}」，不同来源说法不一致：{vs}",
        })
    # 冲突多的排前面
    conflicts.sort(key=lambda c: len(c["claims"]), reverse=True)
    return conflicts[:5]
