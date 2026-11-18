"""HTTP REST 服务(标准库实现, 零额外依赖)。

启动::

    isearch --serve 8787
    python -m intellisearch.server.http_api --port 8787

接口::

    GET  /health
    GET  /stats
    GET  /search?q=...&top_k=5&freshness=1w&format=json|context|markdown
    POST /search      {"query": "...", "top_k": 5, ...}
    GET  /fetch?url=...
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..ai.formatter import compress, to_markdown
from ..config import Config, DEFAULT_CONFIG
from ..engine import SearchEngine
from ..models import Freshness, QueryOptions, Status

log = logging.getLogger("intellisearch.http_api")


class _EngineHolder:
    """进程内共享一个引擎实例(复用连接池/缓存)。"""

    def __init__(self, cfg: Config = None):
        self.cfg = cfg or DEFAULT_CONFIG
        self.engine: Optional[SearchEngine] = None
        self._lock = threading.Lock()

    def get(self) -> SearchEngine:
        if self.engine is None:
            with self._lock:
                if self.engine is None:
                    self.engine = SearchEngine(self.cfg)
        return self.engine


def _opts_from(params: Dict[str, Any]) -> QueryOptions:
    def g(k, default=None):
        v = params.get(k, default)
        if isinstance(v, list):
            v = v[0] if v else default
        return v

    fresh = g("freshness", "any")
    try:
        freshness = Freshness(fresh)
    except ValueError:
        freshness = Freshness.ANY
    excl = g("exclude") or g("exclude_sites") or ""
    return QueryOptions(
        top_k=int(g("top_k", 10) or 10),
        freshness=freshness,
        site=g("site") or None,
        exclude_sites=[s.strip() for s in str(excl).split(",") if s.strip()],
        lang=g("lang", "zh") or "zh",
        timeout=float(g("timeout", 15) or 15),
        retries=int(g("retries", 2) or 2),
        fetch_content=str(g("fetch", "0")).lower() in ("1", "true", "yes"),
        max_content_chars=int(g("max_content", 4000) or 4000),
        session_id=g("session") or None,
        use_cache=str(g("cache", "1")).lower() not in ("0", "false", "no"),
        providers=([s.strip() for s in str(g("providers", "")).split(",")
                    if s.strip()] or None),
    )


def _search_http_status(status: Status) -> int:
    """把检索状态映射为 HTTP 状态码。

    NO_RESULTS 是"源都正常响应、但确实没有匹配结果"的正常业务状态, 必须
    返回 200; 用 SearchResponse.ok 判码会把它当 502, 让调用方(尤其是只看
    状态码的网关/客户端)误以为服务故障并重试。真正的故障/部分成功与
    "没结果"的区分由响应体的 status / ok 字段承担。
    """
    if status in (Status.OK, Status.PARTIAL, Status.NO_RESULTS):
        return 200
    if status == Status.BLOCKED:
        return 403
    return 502


def make_handler(holder: _EngineHolder):
    class Handler(BaseHTTPRequestHandler):
        server_version = "IntelliSearch/1.0"

        # ---------- 基础 ----------
        def _cors_origin(self) -> Optional[str]:
            """按配置决定 CORS 来源: '*' 通配, 否则校验 Origin 是否在白名单。"""
            cfg = getattr(holder, "cfg", None)   # 兼容测试里只提供 get() 的 FakeHolder
            origins = (getattr(cfg, "cors_origins", None) or []) if cfg is not None else []
            if "*" in origins:
                return "*"
            origin = self.headers.get("Origin")
            if origin and origin in origins:
                return origin
            return None

        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            origin = self._cors_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type, Authorization, X-API-Key")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _authorized(self) -> bool:
            """可选 API token 鉴权。token 未配置时放行(本地默认开发模式)。"""
            cfg = getattr(holder, "cfg", None)   # 兼容测试里只提供 get() 的 FakeHolder
            token = getattr(cfg, "api_token", "") if cfg is not None else ""
            if not token:
                return True
            supplied = (self.headers.get("Authorization", "").removeprefix("Bearer ")
                        or self.headers.get("X-API-Key", ""))
            import hmac
            return hmac.compare_digest(supplied, token)

        def _json(self, code: int, obj: Any):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def log_message(self, fmt, *args):     # 静音默认访问日志
            log.debug("%s - %s", self.address_string(), fmt % args)

        def do_OPTIONS(self):
            self._send(204, b"")

        # ---------- 路由 ----------
        def do_GET(self):
            u = urlparse(_decode_path(self.path))
            path = u.path.rstrip("/") or "/"
            q = parse_qs(u.query)
            try:
                if path not in ("/health", "/ping") and not self._authorized():
                    self._json(401, {"ok": False, "message": "未授权: 需要有效的 API token"})
                    return
                if path in ("/health", "/ping"):
                    self._json(200, {"ok": True, "service": "intellisearch",
                                     "version": "1.0.1"})
                elif path == "/stats":
                    self._json(200, holder.get().stats())
                elif path == "/search":
                    self._do_search(q)
                elif path == "/fetch":
                    url = (q.get("url") or [""])[0]
                    if not url:
                        self._json(400, {"ok": False, "message": "缺少 url 参数"})
                        return
                    mode = (q.get("mode") or ["auto"])[0]
                    if mode not in ("auto", "http", "render"):
                        self._json(400, {"ok": False,
                                         "message": "mode 只能是 auto/http/render"})
                        return
                    out = holder.get().fetch(
                        url, max_chars=int((q.get("max_content") or ["4000"])[0]),
                        mode=mode)
                    self._json(200 if out.get("ok") else 502, out)
                elif path == "/render-info":
                    self._json(200, holder.get().fetcher.info())
                elif path == "/":
                    self._send(200, _INDEX.encode("utf-8"), "text/html")
                else:
                    self._json(404, {"ok": False, "message": f"未找到路由 {path}"})
            except Exception as e:                       # noqa: BLE001
                log.exception("GET %s 失败", self.path)
                self._json(500, {"ok": False, "message": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            u = urlparse(_decode_path(self.path))
            path = u.path.rstrip("/") or "/"
            if path != "/search":
                self._json(404, {"ok": False, "message": f"未找到路由 {path}"})
                return
            if not self._authorized():
                self._json(401, {"ok": False, "message": "未授权: 需要有效的 API token"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            _cfg = getattr(holder, "cfg", None)
            max_bytes = getattr(_cfg, "max_body_bytes", 1_000_000) if _cfg is not None else 1_000_000
            if length > max_bytes:
                self._json(413, {"ok": False,
                                 "message": f"请求体过大(上限 {max_bytes} 字节)"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:                            # noqa: BLE001
                self._json(400, {"ok": False, "message": "请求体不是合法 JSON"})
                return
            try:
                self._do_search(body)
            except Exception as e:                       # noqa: BLE001
                log.exception("POST /search 失败")
                self._json(500, {"ok": False, "message": f"{type(e).__name__}: {e}"})

        # ---------- 检索 ----------
        def _do_search(self, params: Dict[str, Any]):
            query = ""
            if isinstance(params.get("q"), list):
                query = params["q"][0] if params["q"] else ""
            query = query or params.get("query") or params.get("q") or ""
            query = str(query).strip()
            if not query:
                self._json(400, {"ok": False, "message": "缺少查询参数 q / query"})
                return
            opts = _opts_from(params)
            fmt = params.get("format", "json")
            if isinstance(fmt, list):
                fmt = fmt[0] if fmt else "json"
            resp = holder.get().search(query, opts)
            # 三种输出格式共用同一套状态码映射: HTTP 码表达"请求是否被正常处理",
            # 业务状态由 body / 文本里的 status 字段表达。否则会出现
            # JSON 分支 error→502、context 分支 error→200 的口径分裂。
            http_code = _search_http_status(resp.status)
            if fmt == "context":
                max_chars = int(params.get("max_chars", 6000) if not isinstance(
                    params.get("max_chars"), list) else
                    (params.get("max_chars") or [6000])[0])
                self._send(http_code, resp.to_context(
                    max_chars=max_chars,
                    with_content=opts.fetch_content).encode("utf-8"),
                    "text/plain")
                return
            if fmt == "markdown":
                self._send(http_code, to_markdown(resp).encode("utf-8"),
                           "text/markdown")
                return
            payload = resp.to_dict()
            max_chars = params.get("max_chars")
            if isinstance(max_chars, list):
                max_chars = max_chars[0] if max_chars else None
            if max_chars:
                payload = compress(payload, int(max_chars))
            self._json(http_code, payload)

    return Handler


def _decode_path(raw: str) -> str:
    """还原 URL 中的非 ASCII 字符。

    http.server 用 ISO-8859-1 解码请求行, 未 percent-encode 的 UTF-8 中文
    会变成乱码(如 "架构" -> "æ¶æ")。这里还原为 UTF-8。
    """
    try:
        return raw.encode("latin-1", errors="ignore").decode("utf-8", errors="replace")
    except Exception:                                # noqa: BLE001
        return raw


_INDEX = """<!doctype html><meta charset="utf-8">
<title>IntelliSearch</title>
<style>body{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:760px;
margin:40px auto;padding:0 20px;line-height:1.6;color:#222}
code{background:#f4f4f6;padding:2px 6px;border-radius:4px}
h1{font-size:22px}</style>
<h1>IntelliSearch · 联网检索服务</h1>
<p>接口：</p>
<ul>
<li><code>GET /search?q=关键词&amp;top_k=5&amp;format=json|context|markdown</code></li>
<li><code>POST /search</code> — body: <code>{"query":"...","top_k":5}</code></li>
<li><code>GET /fetch?url=https://...</code> — 抓取并抽取正文</li>
<li><code>GET /stats</code> · <code>GET /health</code></li>
</ul>
"""


def serve(host: str = "127.0.0.1", port: int = 8787, cfg: Config = None):
    """阻塞启动服务。"""
    holder = _EngineHolder(cfg)
    httpd = ThreadingHTTPServer((host, port), make_handler(holder))
    print(f"IntelliSearch 服务已启动: http://{host}:{port}")
    print("  GET /search?q=...&top_k=5   |   POST /search   |   GET /stats")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
        if holder.engine:
            holder.engine.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    serve(a.host, a.port)
