"""命令行入口。

    isearch "铭凡 UM880 Pro 的 NPU 算力多少"
    isearch "..." --top-k 5 --freshness 1w --json
    isearch "..." --context          # 输出可直接塞进 prompt 的引用文本
    isearch --fetch https://example.com
    isearch --serve 8787             # 启动 HTTP 服务
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .config import Config
from .engine import SearchEngine
from .models import Freshness, QueryOptions, Status


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="isearch",
        description="IntelliSearch - 面向大模型的联网检索工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("query", nargs="?", help="检索词或自然语言问题")
    p.add_argument("-k", "--top-k", type=int, default=10, help="返回条数上限(默认 10)")
    p.add_argument("--freshness", choices=["1d", "1w", "1m", "1y", "any"],
                   default="any", help="时效性过滤")
    p.add_argument("--site", help="只搜指定域名, 如 github.com")
    p.add_argument("--exclude", help="排除域名, 逗号分隔")
    p.add_argument("--providers", help="指定检索源, 逗号分隔(默认 bing_rss,bing_html,sogou,so360)")
    p.add_argument("--lang", default="zh", choices=["zh", "en"])
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--retries", type=int, default=2)

    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="输出完整 JSON(默认)")
    g.add_argument("--context", action="store_true", help="输出带引用编号的上下文文本")
    g.add_argument("--markdown", action="store_true", help="输出 Markdown")
    g.add_argument("--brief", action="store_true", help="只输出标题/链接/时间")

    p.add_argument("--fetch", action="store_true",
                   help="抓取正文(较慢, 但结果可直接作为上下文)")
    p.add_argument("--max-content", type=int, default=4000, help="单页正文最大字符数")
    p.add_argument("--max-chars", type=int, default=0, help="压缩输出到指定字符数")
    p.add_argument("--session", help="会话 ID, 启用多轮上下文记忆")
    p.add_argument("--no-cache", action="store_true", help="跳过缓存")
    p.add_argument("--fetch-url", metavar="URL", help="直接抓取并抽取某个页面")
    p.add_argument("--render", choices=["auto", "http", "render"], default="auto",
                   help="抓取模式: auto(默认, 需要时才渲染) / http(仅轻量) / render(强制渲染)")
    p.add_argument("--serve", metavar="PORT", type=int, help="启动 HTTP 服务")
    p.add_argument("--mcp", action="store_true", help="以 MCP stdio 服务模式启动")
    p.add_argument("--stats", action="store_true", help="查看运行状态")
    p.add_argument("--purge-cache", action="store_true", help="清空缓存")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    # Windows 在非 UTF-8 代码页(如 en-US 的 cp1252)下, 打印中文帮助/结果会
    # UnicodeEncodeError 直接崩 —— GitHub 托管的 Windows runner 就是该配置,
    # 真实用户装在英文系统上同样会踩。统一按 UTF-8 输出(无法编码的字符降级
    # 而非异常); TTY 场景不受影响(Windows 控制台内部本就走 UTF-16 API),
    # 管道下游(尤其喂给大模型)拿到的也始终是 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    args = _build_parser().parse_args(argv)

    if args.verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = Config()
    if args.providers:
        cfg.providers = [s.strip() for s in args.providers.split(",") if s.strip()]

    # MCP 模式: 由 mcp_server 接管 stdin/stdout
    if args.mcp:
        from .server.mcp_server import run_stdio
        return run_stdio(cfg)

    if args.serve:
        from .server.http_api import serve
        serve(port=args.serve, cfg=cfg)
        return 0

    engine = SearchEngine(cfg)

    try:
        if args.stats:
            print(json.dumps(engine.stats(), ensure_ascii=False, indent=2))
            return 0

        if args.purge_cache:
            n = engine.cache.purge()
            print(f"已清空缓存 {n} 条")
            return 0

        if args.fetch_url:
            out = engine.fetch(args.fetch_url, max_chars=args.max_content,
                               mode=args.render)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out.get("ok") else 1

        if not args.query:
            _build_parser().print_help()
            return 2

        opts = QueryOptions(
            top_k=args.top_k,
            freshness=Freshness(args.freshness),
            site=args.site,
            exclude_sites=[s.strip() for s in (args.exclude or "").split(",") if s.strip()],
            lang=args.lang,
            timeout=args.timeout,
            retries=args.retries,
            fetch_content=args.fetch,
            max_content_chars=args.max_content,
            session_id=args.session,
            use_cache=not args.no_cache,
        )
        resp = engine.search(args.query, opts)

        if args.context:
            print(resp.to_context(
                max_chars=args.max_chars or 6000,
                with_content=args.fetch))
        elif args.markdown:
            from .ai.formatter import to_markdown
            print(to_markdown(resp))
        elif args.brief:
            for i, r in enumerate(resp.results, 1):
                t = (r.publish_time or "")[:10]
                print(f"{i:2d}. [{t}] {r.title}\n     {r.url}")
            if not resp.results:
                print(f"无结果（状态: {resp.status.value}）{resp.message}")
        else:
            payload = resp.to_dict()
            if args.max_chars:
                from .ai.formatter import compress
                payload = compress(payload, args.max_chars)
            print(json.dumps(payload, ensure_ascii=False, indent=2))

        return 0 if resp.ok else (1 if resp.status != Status.NO_RESULTS else 0)
    finally:
        engine.close()


if __name__ == "__main__":
    sys.exit(main())
