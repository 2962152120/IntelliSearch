"""极简 HTML DOM —— 零第三方依赖。

为什么不直接用 BeautifulSoup: 本工具要能低依赖地跑在任意环境里,
而我们需要的能力(遍历/查 class/取文本)用标准库 html.parser 完全够用。

关键设计: **文本片段也是节点**(tag == "")。
搜索结果页普遍用 <em>/<strong> 包裹命中的查询词, 若把文本和高亮标签
分开存储就会丢失文档顺序, 导致标题错位(如"铭凡 UM880 Pro" 被丢到句尾)。
把文本也建成节点后, text_content() 天然按文档顺序还原。

其它保证:
- 跳过 script/style/noscript/template 等噪声内容;
- 块级标签产生换行, 保留段落边界;
- 遍历为迭代实现, 深嵌套文档不会递归爆栈。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Iterator, List, Optional

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
SKIP_CONTENT_TAGS = {"script", "style", "noscript", "template", "svg", "canvas",
                     "iframe", "object", "video", "audio", "map"}
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "div", "dl", "dt", "dd",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "td", "th", "tr", "ul", "option", "details", "summary",
}


class Node:
    __slots__ = ("tag", "attrs", "children", "text", "parent", "depth")

    def __init__(self, tag: str = "", attrs: dict = None,
                 parent: "Node" = None, depth: int = 0, text: str = ""):
        self.tag = (tag or "").lower()
        self.attrs: dict = attrs or {}
        self.children: List["Node"] = []
        self.text = text            # 仅文本节点(tag == "")使用
        self.parent = parent
        self.depth = depth

    # ---------- 属性 ----------
    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)

    @property
    def classes(self) -> List[str]:
        return (self.attrs.get("class") or "").split()

    def has_class(self, *names: str) -> bool:
        cls = set(self.classes)
        return any(n in cls for n in names)

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def is_text(self) -> bool:
        return self.tag == ""

    # ---------- 遍历(迭代实现, 不递归) ----------
    def iter_nodes(self) -> Iterator["Node"]:
        stack: List[Node] = [self]
        while stack:
            n = stack.pop()
            yield n
            if n.children:
                stack.extend(reversed(n.children))

    def find_all(self, tag: str = None, cls: str = None,
                 limit: int = 0) -> List["Node"]:
        """按 tag / class(包含匹配) 查询, 深度优先、按文档顺序。"""
        want_tag = tag.lower() if tag else None
        out: List[Node] = []
        for n in self.iter_nodes():
            if n is self or n.is_text:
                continue
            if want_tag and n.tag != want_tag:
                continue
            if cls and cls not in n.classes:
                continue
            out.append(n)
            if limit and len(out) >= limit:
                break
        return out

    def find(self, tag: str = None, cls: str = None) -> Optional["Node"]:
        r = self.find_all(tag=tag, cls=cls, limit=1)
        return r[0] if r else None

    # ---------- 文本 ----------
    def text_content(self) -> str:
        """按文档顺序还原文本, 块级标签处补换行。"""
        parts: List[str] = []
        stack: List[tuple] = [(self, False)]     # (node, 已展开)
        while stack:
            n, expanded = stack.pop()
            if expanded:
                if n.tag in BLOCK_TAGS:
                    parts.append("\n")
                continue
            if n.is_text:
                parts.append(n.text)
                continue
            if n.tag in BLOCK_TAGS:
                parts.append("\n")
            stack.append((n, True))
            for c in reversed(n.children):
                stack.append((c, False))
        return "".join(parts)

    @property
    def own_text(self) -> str:
        """直接子文本(不含后代元素文本)。"""
        return "".join(c.text for c in self.children if c.is_text)

    def __repr__(self):
        if self.is_text:
            return f"<Text {self.text[:20]!r}>"
        cls = ".".join(self.classes[:2])
        return f"<Node {self.tag}{'.' + cls if cls else ''} children={len(self.children)}>"


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack: List[Node] = [self.root]
        self._skip_depth = 0
        self._skip_tag = ""

    @property
    def current(self) -> Node:
        return self.stack[-1]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_depth:
            return
        if tag in SKIP_CONTENT_TAGS:
            self._skip_depth = len(self.stack)
            self._skip_tag = tag
            return
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs},
                    self.current, len(self.stack))
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_CONTENT_TAGS:
            return
        self.current.children.append(
            Node(tag, {k.lower(): (v or "") for k, v in attrs},
                 self.current, len(self.stack)))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_depth:
            if tag == self._skip_tag and len(self.stack) >= self._skip_depth:
                self._skip_depth = 0
                self._skip_tag = ""
            return
        if tag in VOID_TAGS:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self._skip_depth or not data:
            return
        # 文本片段作为独立节点, 保证文档顺序不丢失
        self.current.children.append(Node("", None, self.current,
                                          len(self.stack), data))


def parse_html(html: str) -> Node:
    """解析 HTML 为 DOM 树。空/异常输入返回空 root(绝不抛异常)。"""
    if not html:
        return Node("root")
    b = _Builder()
    try:
        b.feed(html)
        b.close()
    except Exception:      # noqa: BLE001 - 极端畸形输入下 HTMLParser 可能抛错
        pass
    return b.root


def strip_tags(html: str) -> str:
    """快速去标签取文本(不需要 DOM 时用这个, 更快)。"""
    if not html:
        return ""
    s = re.sub(r"(?is)<(script|style|noscript|template|svg)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&ldquo;", "“"),
                 ("&rdquo;", "”"), ("&mdash;", "—"), ("&hellip;", "…")):
        s = s.replace(a, b)
    return re.sub(r"[ \t\u00a0]+", " ", s)


def normalize_text(s: str) -> str:
    """文本降噪: 合并空白、压缩多余空行。"""
    if not s:
        return ""
    s = s.replace("\u00a0", " ").replace("\u3000", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*", "\n", s)
    return "\n".join(line.strip() for line in s.split("\n")).strip()
