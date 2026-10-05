"""正文抽取与文本清洗测试。"""
from intellisearch.extract.clean import (clean_snippet, detect_source_type,
                                         extract_date, normalize_text,
                                         strip_date_prefix, summarize, truncate)
from intellisearch.extract.extractor import extract
from intellisearch.models import SourceType

from conftest import load_page


ARTICLE = """
<html><head><title>测试文章 - 某站点</title>
<meta property="article:published_time" content="2026-03-05T10:00:00+00:00">
<meta name="author" content="张三">
</head><body>
<nav>首页 关于 联系我们</nav>
<div class="content">
  <h1>测试标题</h1>
  <p>这是第一段正文内容，它有足够的长度来被识别为正文段落，而不是导航栏。</p>
  <p>这是第二段正文内容，同样具有足够的长度，用于验证段落切分是否正常工作。</p>
  <p>这是第三段，包含一些技术参数：算力为 50 TOPS，功耗 28W。</p>
</div>
<div class="sidebar">相关推荐 广告位</div>
<footer>版权所有</footer>
</body></html>
"""


def test_extract_finds_main_content():
    page = extract(ARTICLE, url="https://a.com/x")
    assert "第一段正文内容" in page.clean_text
    assert "第三段" in page.clean_text


def test_extract_excludes_nav_and_footer():
    page = extract(ARTICLE, url="https://a.com/x")
    assert "联系我们" not in page.clean_text
    assert "版权所有" not in page.clean_text
    assert "相关推荐" not in page.clean_text


def test_extract_meta_publish_time_and_author():
    page = extract(ARTICLE, url="https://a.com/x")
    assert page.publish_time is not None
    assert page.publish_time.startswith("2026-03-05")
    assert page.author == "张三"


def test_extract_segments_are_numbered():
    page = extract(ARTICLE, url="https://a.com/x")
    assert len(page.segments) >= 2
    assert page.segments[0]["index"] == 1
    assert "text" in page.segments[0]


def test_extract_respects_max_chars():
    page = extract(ARTICLE, url="https://a.com/x", max_chars=60)
    assert len(page.clean_text) <= 80
    assert page.truncated is True


def test_extract_empty_input_is_safe():
    page = extract("", url="")
    assert page.clean_text == ""
    assert page.segments == []


def test_extract_real_doc_page():
    """用真实的技术文档页验证抽取效果。"""
    page = extract(load_page("page_doc.html"),
                   url="https://docs.python.org/zh-cn/3/tutorial/datastructures.html")
    assert len(page.clean_text) > 500, f"正文过短: {len(page.clean_text)}"
    assert page.source_type == SourceType.DOC
    assert page.title


def test_strip_date_prefix():
    d, rest = strip_date_prefix("2026年9月24日 · 正文内容")
    assert d and rest == "正文内容"


def test_extract_date_from_text():
    assert extract_date("发布于 2026-01-15") is not None
    assert extract_date("3天前") is not None


def test_clean_snippet_removes_tail_date():
    s = clean_snippet("正文内容很长很长的一段话需要保留下来啊啊啊 … 2025年9月27日 - ")
    assert "2025年9月27日" not in s


def test_clean_snippet_deduplicates_sentences():
    s = clean_snippet("重复句子。重复句子。不同句子。")
    assert s.count("重复句子") == 1


def test_summarize_prefers_informative_sentences():
    text = "这是一句普通的话。" * 20 + "算力为 50 TOPS，这是关键参数。"
    s = summarize(text, 200)
    assert "50" in s or "TOPS" in s


def test_truncate_breaks_at_punctuation():
    out = truncate("一二三四五。六七八九十。", 7)
    assert out.endswith("…") or len(out) <= 12


def test_detect_source_type():
    assert detect_source_type("https://github.com/a/b", "") == SourceType.CODE
    assert detect_source_type("https://docs.python.org/x", "") == SourceType.DOC
    assert detect_source_type("https://zh.wikipedia.org/x", "") == SourceType.WIKI
    assert detect_source_type("https://a.com/file.pdf", "") == SourceType.PDF
    assert detect_source_type("https://stackoverflow.com/q/1", "") == SourceType.FORUM


def test_normalize_text_handles_blank_lines():
    assert normalize_text("a\n\n\nb") == "a\nb"
