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


def extract_facts(text: str, relevant_terms: Sequence[str] = ()) -> List[Tuple[str, float, str]]:
    """抽取 (关键词, 数值, 单位) 三元组。

    relevant_terms 非空时, 只保留关键词与查询词相关的事实。
    """
    if not text:
        return []
    rel = {_norm_key(t) for t in relevant_terms if t}
    out: List[Tuple[str, float, str]] = []
    for m in FACT_RE.finditer(text[:4000]):
        key, val, unit = _norm_key(m.group(1)), float(m.group(2)), m.group(3).upper()
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
