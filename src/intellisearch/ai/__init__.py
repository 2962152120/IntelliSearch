"""AI 适配层：输出格式化 / 冲突检测 / 会话上下文。"""
from .formatter import build_context, compress, ensure_schema, summarize_results, to_markdown
from .conflict import detect_conflicts, extract_facts
from .context import SessionStore, apply_context, needs_context

__all__ = ["build_context", "compress", "ensure_schema", "summarize_results",
           "to_markdown", "detect_conflicts", "extract_facts",
           "SessionStore", "apply_context", "needs_context"]
