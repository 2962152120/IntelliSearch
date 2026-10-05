"""MCP stdio 服务 —— 让任意支持 MCP 的 AI 客户端直接调用本工具。

启动::

    isearch --mcp

在客户端(如 Claude Desktop / WorkBuddy)中配置::

    {
      "mcpServers": {
        "intellisearch": {
          "command": "python",
          "args": ["-m", "intellisearch.cli", "--mcp"]
        }
      }
    }

暴露的工具:
- intellisearch_search  联网检索(返回结构化 JSON 或上下文文本)
- intellisearch_fetch   抓取指定 URL 并抽取正文
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from ..config import Config
from ..engine import SearchEngine
from ..models import Freshness, QueryOptions

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "intellisearch", "version": "1.0.0"}


def _force_utf8_stdio() -> None:
    """把 stdin/stdout 强制为 UTF-8。

    中文 Windows 的默认 locale 编码是 GBK(cp936)。若不强制, 工具描述和
    检索结果里的中文会按 GBK 写进 stdout, 而 MCP 客户端一律按 UTF-8 解码,
    结果是 UnicodeDecodeError 把客户端打挂 —— 且不依赖客户端是否设置
    PYTHONUTF8。stderr 一并处理, 避免日志里的中文变成乱码。
    """
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass        # 老版本 Python 或被重定向时不支持, 忽略

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "intellisearch_search",
        "description": (
            "联网检索实时信息。输入关键词或自然语言问题，返回结构化的网页结果"
            "（标题、链接、摘要、发布时间、来源类型、相关性得分），"
            "可直接作为回答的依据并标注引用。支持时效性过滤、站点限定、结果数量控制。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词或自然语言问题"},
                "top_k": {"type": "integer", "description": "返回条数，1-50，默认 8",
                          "default": 8},
                "freshness": {"type": "string", "description":
                              "时效性: 1d/1w/1m/1y/any，默认 any", "default": "any"},
                "site": {"type": "string", "description": "限定域名，如 github.com"},
                "fetch_content": {"type": "boolean",
                                  "description": "是否抓取正文(更慢但信息更全)",
                                  "default": False},
                "format": {"type": "string",
                           "description": "输出格式: json | context，默认 json",
                           "default": "json"},
                "max_chars": {"type": "integer",
                              "description": "输出最大字符数，超出自动压缩",
                              "default": 6000},
            },
            "required": ["query"],
        },
    },
    {
        "name": "intellisearch_fetch",
        "description": "抓取指定网页并抽取正文、标题、发布时间，用于深入阅读某个检索结果。"
                       "默认先用轻量 HTTP，若页面是 JS 动态渲染(SPA)则自动升级为"
                       "内置 Chromium 无头浏览器渲染。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的网页地址"},
                "max_chars": {"type": "integer", "description": "正文最大字符数",
                              "default": 4000},
                "mode": {"type": "string",
                         "description": "抓取模式: auto(默认, 需要时才渲染) / "
                                        "http(仅轻量, 最快) / render(强制渲染)",
                         "enum": ["auto", "http", "render"],
                         "default": "auto"},
            },
            "required": ["url"],
        },
    },
]


class MCPServer:
    """JSON-RPC over stdio。日志一律走 stderr, 保证 stdout 只有协议消息。"""

    def __init__(self, cfg: Config = None):
        self.cfg = cfg or Config()
        self.engine = SearchEngine(self.cfg)

    # ---------- 主循环 ----------
    def run(self) -> int:
        # MCP 客户端一律按 UTF-8 解析 stdout。中文 Windows 下 Python 默认
        # 用 GBK 编码 stdout, 工具描述/检索结果里的中文会输出成 GBK 字节,
        # 客户端按 UTF-8 解码直接 UnicodeDecodeError —— 必须强制 UTF-8。
        _force_utf8_stdio()
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp = self.handle(req)
            if resp is None:
                continue                      # 通知类消息不回包
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        return 0

    # ---------- 分发 ----------
    def handle(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = req.get("method", "")
        rid = req.get("id")
        params = req.get("params") or {}

        if method == "initialize":
            return self._ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            })
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "tools/list":
            return self._ok(rid, {"tools": TOOLS})
        if method == "tools/call":
            return self._call_tool(rid, params)
        if method == "ping":
            return self._ok(rid, {})
        if rid is None:
            return None
        return self._err(rid, -32601, f"不支持的方法: {method}")

    def _call_tool(self, rid, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            if name == "intellisearch_search":
                text = self._tool_search(args)
            elif name == "intellisearch_fetch":
                text = self._tool_fetch(args)
            else:
                return self._err(rid, -32602, f"未知工具: {name}")
            return self._ok(rid, {"content": [{"type": "text", "text": text}],
                                  "isError": False})
        except Exception as e:                       # noqa: BLE001
            return self._ok(rid, {
                "content": [{"type": "text",
                             "text": f"工具执行失败: {type(e).__name__}: {e}"}],
                "isError": True})

    # ---------- 工具实现 ----------
    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "错误: 缺少 query 参数"
        try:
            freshness = Freshness(str(args.get("freshness", "any")))
        except ValueError:
            freshness = Freshness.ANY
        opts = QueryOptions(
            top_k=int(args.get("top_k", 8) or 8),
            freshness=freshness,
            site=args.get("site") or None,
            fetch_content=bool(args.get("fetch_content", False)),
            max_content_chars=int(args.get("max_chars", 6000) or 6000) // 2,
        )
        resp = self.engine.search(query, opts)
        if str(args.get("format", "json")) == "context":
            return self.engine.search_context(
                query, opts, max_chars=int(args.get("max_chars", 6000) or 6000))
        payload = resp.to_dict()
        from ..ai.formatter import compress
        payload = compress(payload, int(args.get("max_chars", 6000) or 6000))
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _tool_fetch(self, args: Dict[str, Any]) -> str:
        url = str(args.get("url", "")).strip()
        if not url:
            return "错误: 缺少 url 参数"
        mode = str(args.get("mode", "auto"))
        if mode not in ("auto", "http", "render"):
            return "错误: mode 只能是 auto / http / render"
        out = self.engine.fetch(url, max_chars=int(args.get("max_chars", 4000)),
                                mode=mode)
        return json.dumps(out, ensure_ascii=False, indent=2)

    # ---------- 响应构造 ----------
    @staticmethod
    def _ok(rid, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _err(rid, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": code, "message": message}}


def run_stdio(cfg: Config = None) -> int:
    srv = MCPServer(cfg)
    try:
        return srv.run()
    finally:
        srv.engine.close()


if __name__ == "__main__":
    sys.exit(run_stdio())
