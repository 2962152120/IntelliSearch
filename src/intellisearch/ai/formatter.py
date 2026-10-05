"""输出格式化与压缩(需求四.1 / 四.5)。

- 结构化 JSON(字段固定, 大模型可直接解析)
- 摘要预提取(不调 LLM, 抽取式)
- 按目标上下文窗口压缩输出
- Markdown / 带引用编号的纯文本两种形态
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..extract.clean import summarize, truncate
from ..models import SearchResponse, SearchResult

REQUIRED_FIELDS = ("title", "url", "publish_time", "source_type",
                   "snippet", "relevance_score")


def ensure_schema(r: SearchResult) -> Dict[str, Any]:
    """保证输出字段齐备(缺失补默认值, 类型稳定)。"""
    return {
        "title": r.title or "",
        "url": r.url or "",
        "publish_time": r.publish_time,
        "source_type": (r.source_type.value if hasattr(r.source_type, "value")
                        else str(r.source_type)),
        "snippet": r.snippet or "",
        "relevance_score": round(float(r.relevance_score or 0.0), 4),
        "credibility": round(float(r.credibility or 0.0), 4),
        "domain": r.domain or "",
        "sources": r.extra.get("sources") or ([r.source] if r.source else []),
        "clean_text": r.clean_text or "",
        "segments": r.segments or [],
    }


def summarize_results(results: Sequence[SearchResult], max_chars: int = 300,
                      prefer_content: bool = True) -> None:
    """为每条结果生成摘要(原地写入 snippet_extra / 覆盖过长的 snippet)。"""
    for r in results:
        base = r.clean_text if (prefer_content and r.clean_text) else r.snippet
        s = summarize(base or r.snippet or "", max_chars)
        r.extra["summary"] = s
        if not r.snippet:
            r.snippet = s


def compress(payload: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    """把结构化输出压缩到目标字符数内。

    策略: 先裁每条的正文, 再裁摘要, 最后才减少条数 —— 保证信息覆盖面。
    """
    if max_chars <= 0:
        return payload
    def size(p):
        import json
        return len(json.dumps(p, ensure_ascii=False))

    if size(payload) <= max_chars:
        return payload

    # 1. 丢弃 clean_text / segments
    for r in payload.get("results", []):
        r.pop("clean_text", None)
        r.pop("segments", None)
    if size(payload) <= max_chars:
        return payload

    # 2. 逐步压缩摘要
    for limit in (400, 240, 160, 100, 60):
        for r in payload.get("results", []):
            s = r.get("snippet", "")
            if len(s) > limit:
                r["snippet"] = s[:limit] + "…"
        if size(payload) <= max_chars:
            payload["compressed"] = True
            return payload

    # 3. 减少条数, 但至少保留 1 条
    results = payload.get("results", [])
    while len(results) > 1 and size(payload) > max_chars:
        results.pop()
    payload["results"] = results
    payload["compressed"] = True
    payload["truncated"] = True
    return payload


def to_markdown(resp: SearchResponse, with_content: bool = False,
                max_body: int = 600) -> str:
    """Markdown 形态, 便于直接贴进文档或 Chat 应用。"""
    lines = [f"### 检索结果：{resp.query}",
             f"- 状态：`{resp.status.value}`，共 {len(resp.results)} 条，"
             f"耗时 {resp.elapsed_ms} ms"]
    if resp.message:
        lines.append(f"- 说明：{resp.message}")
    lines.append("")
    for i, r in enumerate(resp.results, 1):
        meta = [r.domain]
        if r.publish_time:
            meta.append((r.publish_time or "")[:10])
        meta.append(r.source_type.value if hasattr(r.source_type, "value")
                    else str(r.source_type))
        lines.append(f"{i}. **[{r.title}]({r.url})**  ")
        lines.append(f"   `{' · '.join(meta)}`")
        body = r.clean_text if (with_content and r.clean_text) else r.snippet
        if body:
            lines.append(f"   {truncate(body, max_body, '')}")
        lines.append("")
    if resp.conflicts:
        lines.append("> ⚠️ 信息差异")
        for c in resp.conflicts:
            lines.append(f"> - {c['message']}")
    return "\n".join(lines)


def build_context(results: Sequence[SearchResult], max_chars: int = 6000,
                  with_content: bool = False,
                  snippet_chars: int = 300) -> str:
    """带 [n] 编号的上下文文本, 便于大模型回答时标注引用。"""
    lines: List[str] = []
    used = 0
    for i, r in enumerate(results, 1):
        body = r.clean_text if (with_content and r.clean_text) else r.snippet
        body = truncate(body or "", snippet_chars, "")
        meta = r.domain or ""
        if r.publish_time:
            meta += f" / {(r.publish_time or '')[:10]}"
        block = f"[{i}] {r.title} ({meta})\n    {r.url}\n    {body}"
        if used + len(block) > max_chars:
            if used == 0:
                lines.append(block[:max_chars])
            break
        lines.append(block)
        used += len(block) + 1
    return "\n".join(lines)
