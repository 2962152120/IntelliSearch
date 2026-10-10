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
