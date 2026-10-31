"""CLI 入口测试。"""
import os
import subprocess
import sys

from conftest import ROOT


def test_cli_help_survives_non_utf8_codepage():
    """非 UTF-8 代码页(如 Windows cp1252)下打印中文帮助不能崩。

    真实事故: GitHub 托管的 Windows runner 是 en-US/cp1252, CI 里的
    `isearch --help` 直接 UnicodeEncodeError 退出码 1; 英文版 Windows
    用户同样会踩。PYTHONIOENCODING=cp1252 可在任何平台稳定复现修复前
    的崩溃(修复在 cli.py 的 main() 开头按 UTF-8 重配 stdout)。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, "-m", "intellisearch.cli", "--help"],
                       capture_output=True, cwd=str(ROOT), env=env, timeout=120)
    assert r.returncode == 0, \
        "cp1252 下 --help 应正常退出, 实际报错:\n" + \
        r.stderr.decode("utf-8", "replace")[-800:]
    out = r.stdout.decode("utf-8", "replace")
    assert "检索" in out, f"中文帮助应以 UTF-8 正常输出, 实际开头: {out[:200]!r}"


def test_cli_serve_passes_host_and_port(monkeypatch):
    """--serve 必须把 --host/--serve 透传给 http_api.serve。

    回归: Dockerfile 曾只传 `--serve 8787` → serve() 默认 host="127.0.0.1",
    容器内只监听回环, docker-compose 的 "8787:8787" 端口映射从宿主机不可达
    (README 5.2 的 curl 连不上)。main() 里是惰性 import, 所以 patch 必须打在
    intellisearch.server.http_api 模块属性上, 才能被 `from ... import serve` 取到。
    """
    from intellisearch import cli
    from intellisearch.server import http_api

    calls = []

    def fake_serve(**kw):
        calls.append(kw)

    monkeypatch.setattr(http_api, "serve", fake_serve)

    assert cli.main(["--serve", "8787", "--host", "0.0.0.0"]) == 0
    assert len(calls) == 1
    assert calls[0]["host"] == "0.0.0.0", \
        f"容器内必须监听 0.0.0.0, 实际传给 serve 的是 {calls[0]['host']!r}"
    assert calls[0]["port"] == 8787


def test_cli_serve_default_host_is_loopback(monkeypatch):
    """不传 --host 时保持 127.0.0.1 默认, 本机运行不对外暴露。"""
    from intellisearch import cli
    from intellisearch.server import http_api

    calls = []

    def fake_serve(**kw):
        calls.append(kw)

    monkeypatch.setattr(http_api, "serve", fake_serve)

    assert cli.main(["--serve", "8787"]) == 0
    assert len(calls) == 1
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 8787
