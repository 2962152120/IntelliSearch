"""内容抽取层：DOM / 正文抽取 / 文本清洗。"""
from .dom import Node, parse_html, strip_tags, normalize_text
from .clean import (clean_snippet, detect_source_type, domain_of, extract_date,
                    is_spam, normalize_text as normalize, root_domain,
                    strip_date_prefix, summarize, truncate)
from .extractor import ExtractedPage, extract

__all__ = ["Node", "parse_html", "strip_tags", "normalize_text", "clean_snippet",
           "detect_source_type", "domain_of", "extract_date", "is_spam", "root_domain",
           "strip_date_prefix", "summarize", "truncate", "ExtractedPage", "extract"]
