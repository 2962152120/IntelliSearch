"""DOM 解析层测试。"""
from intellisearch.extract.dom import parse_html, normalize_text, strip_tags


def test_parse_simple_nesting():
    root = parse_html("<div class='a'><p>hello</p><p>world</p></div>")
    div = root.find("div", "a")
    assert div is not None
    assert len(div.find_all("p")) == 2
    assert normalize_text(div.text_content()) == "hello\nworld"


def test_text_order_preserved_with_highlight_tags():
    """高亮标签不能打乱文本顺序(搜索结果页标题正确性的关键)。"""
    html = '<a><em>铭凡UM880</em> <em>Pro</em>散热大升级,<em>NPU</em>性能大提升</a>'
    a = parse_html(html).find("a")
    assert normalize_text(a.text_content()) == "铭凡UM880 Pro散热大升级,NPU性能大提升"


def test_bing_style_title_order():
    html = '<h2><a href="http://x"><strong>铭</strong>（汉语文字）_百度百科</a></h2>'
    a = parse_html(html).find("a")
    assert normalize_text(a.text_content()) == "铭（汉语文字）_百度百科"


def test_skip_script_and_style():
    html = ("<div><script>var x='<p>noise</p>';</script>"
            "<style>.a{color:red}</style><p>real</p></div>")
    div = parse_html(html).find("div")
    text = normalize_text(div.text_content())
    assert "noise" not in text and "color" not in text
    assert "real" in text


def test_unclosed_tags_do_not_crash():
    root = parse_html("<div><p>a<p>b<span>c")
    assert "a" in normalize_text(root.text_content())
    assert "c" in normalize_text(root.text_content())


def test_empty_and_none_input():
    assert parse_html("").find_all("div") == []
    assert parse_html(None).find_all("div") == []


def test_deep_nesting_no_recursion_error():
    html = "<div>" * 500 + "deep" + "</div>" * 500
    root = parse_html(html)
    assert "deep" in normalize_text(root.text_content())


def test_find_all_by_class_and_limit():
    root = parse_html("<ul><li class='x'>1</li><li class='x'>2</li>"
                      "<li class='y'>3</li></ul>")
    assert len(root.find_all("li", "x")) == 2
    assert len(root.find_all("li", "x", limit=1)) == 1
    assert root.find("li", "y").text_content().strip() == "3"


def test_attributes_and_entities():
    root = parse_html('<a href="http://a.b/c?x=1&amp;y=2" title="T&amp;U">x</a>')
    a = root.find("a")
    assert a.get("href") == "http://a.b/c?x=1&y=2"
    assert a.get("title") == "T&U"
    assert a.get("missing", "dft") == "dft"


def test_strip_tags_removes_markup_and_decodes_entities():
    assert strip_tags("<p>a &amp; b</p>").strip() == "a & b"
    assert strip_tags("<script>x</script><b>hi</b>").strip() == "hi"


def test_normalize_text_collapses_blank_lines():
    assert normalize_text("a\n\n\n\nb") == "a\nb"
    assert normalize_text("  a   b  ") == "a b"
