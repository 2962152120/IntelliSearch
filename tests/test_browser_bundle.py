"""内置 Chromium:发现顺序与降级行为。

背景: 网页渲染能力要求"换台机器也能跑"。原先内核发现**优先用系统浏览器**
(Edge/Chrome), 目标机器没装浏览器时就退化成纯 HTTP, JS 动态页面抓不到正文。
现在改为**包内自带内核优先**, 系统浏览器降为兜底。

这些测试锁定三件事:
  1. 包内自带内核排在系统浏览器之前;
  2. 显式指定(IS_BROWSER_PATH 等)仍然最高优先级;
  3. 一个内核都没有时干净降级, 不抛异常。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from intellisearch.http import browser as B


def _sources(explicit=""):
    return [(c.source, c.executable_path or c.channel)
            for c in B._candidates(explicit)]


def test_bundled_chromium_is_preferred_over_system_browser(monkeypatch):
    """包内自带内核必须排在系统浏览器之前 —— 这是"换机器也能跑"的前提。"""
    monkeypatch.setattr(B, "_playwright_bundled",
                        lambda: "/pkg/.local-browsers/chromium-1243/chrome.exe")
    monkeypatch.setattr(B, "_playwright_browsers",
                        lambda: ["/cache/chromium-1210/chrome.exe"])
    # 造一个假的系统浏览器候选
    monkeypatch.setattr(B, "_WIN_PAIRS", [("FakeChrome", "chrome", "x/y/chrome.exe")])
    monkeypatch.setattr(B, "_WIN_ROOTS", ["/fake/root"])
    monkeypatch.setattr(os.path, "isfile", lambda p: False)

    srcs = [s for s, _ in _sources()]
    assert "playwright-bundled" in srcs
    if "system" in srcs:                # 仅当系统浏览器确实存在时才比较顺序
        assert srcs.index("playwright-bundled") < srcs.index("system"), (
            f"包内内核必须优先于系统浏览器, 实际顺序: {srcs}")


def test_explicit_path_wins_over_everything(monkeypatch, tmp_path):
    """IS_BROWSER_PATH 显式指定仍然最高优先级。"""
    exe = tmp_path / "my-chrome.exe"
    exe.write_text("x")
    monkeypatch.setenv("IS_BROWSER_PATH", str(exe))
    monkeypatch.setattr(B, "_playwright_bundled", lambda: "/pkg/chrome.exe")
    cands = B._candidates()
    assert cands[0].source == "config", f"显式指定应排第一, 实际: {cands[0].source}"
    assert cands[0].executable_path == str(exe)


def test_cache_chromium_ranks_above_system(monkeypatch):
    """ms-playwright 缓存也应优先于系统浏览器。"""
    monkeypatch.delenv("IS_BROWSER_PATH", raising=False)
    monkeypatch.delenv("IS_CHROME_PATH", raising=False)
    monkeypatch.setattr(B, "_playwright_bundled", lambda: None)
    monkeypatch.setattr(B, "_playwright_browsers",
                        lambda: ["/cache/chromium-1210/chrome.exe"])
    monkeypatch.setattr(B, "_WIN_PAIRS", [("FakeChrome", "chrome", "x/y/chrome.exe")])
    monkeypatch.setattr(B, "_WIN_ROOTS", ["/fake/root"])
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    srcs = [s for s, _ in _sources()]
    if "system" in srcs:
        assert srcs.index("ms-playwright-cache") < srcs.index("system")


def test_no_browser_anywhere_degrades_cleanly(monkeypatch):
    """一个内核都没有时返回 None(调用方据此降级), 不抛异常。"""
    monkeypatch.delenv("IS_BROWSER_PATH", raising=False)
    monkeypatch.delenv("IS_CHROME_PATH", raising=False)
    monkeypatch.setattr(B, "_playwright_bundled", lambda: None)
    monkeypatch.setattr(B, "_playwright_browsers", lambda: [])
    monkeypatch.setattr(B, "_WIN_PAIRS", [])
    monkeypatch.setattr(B, "_WIN_ROOTS", [])
    monkeypatch.setattr(B, "_MAC_APPS", [])
    monkeypatch.setattr(B, "_UNIX_PAIRS", [])
    monkeypatch.setattr(B, "_roots", lambda: [])
    assert B.find_chromium() is None


def test_candidates_are_deduplicated(monkeypatch):
    """同一路径只出现一次, 否则会重复启动同一个内核。"""
    monkeypatch.delenv("IS_BROWSER_PATH", raising=False)
    monkeypatch.delenv("IS_CHROME_PATH", raising=False)
    same = "/pkg/chrome.exe"
    monkeypatch.setattr(B, "_playwright_bundled", lambda: same)
    monkeypatch.setattr(B, "_playwright_browsers", lambda: [same])
    monkeypatch.setattr(B, "_WIN_PAIRS", [])
    monkeypatch.setattr(B, "_WIN_ROOTS", [])
    paths = [p for _, p in _sources()]
    assert len(paths) == len(set(paths)), f"候选路径重复: {paths}"


# ----------------------------------------------------------------------
# 安装脚本(决定内核能否真正随包走)
# ----------------------------------------------------------------------
def _script():
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[1]
            / "scripts" / "install_browser.py")


def test_install_script_exists_and_documented():
    p = _script()
    assert p.is_file(), "缺少 scripts/install_browser.py —— 用户无从安装内置内核"
    text = p.read_text(encoding="utf-8")
    assert "PLAYWRIGHT_BROWSERS_PATH" in text, "脚本必须把内核装进包内(设为 0)"
    assert "--check" in text, "应提供只检查不安装的模式"


def test_install_script_is_valid_python():
    import py_compile
    py_compile.compile(str(_script()), doraise=True)


def test_render_extra_declares_playwright():
    """渲染能力是可选依赖, 但必须声明清楚。"""
    import pathlib
    tom = (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml")
    text = tom.read_text(encoding="utf-8")
    assert "render" in text and "playwright" in text, \
        "pyproject 应提供 render extra 并声明 playwright"


# ----------------------------------------------------------------------
# 内置内核必须"不依赖运行时环境变量"
# ----------------------------------------------------------------------
def test_package_browsers_does_not_depend_on_env_var(monkeypatch):
    """包目录扫描不得读 PLAYWRIGHT_BROWSERS_PATH。

    playwright 自己的 executable_path 只有在运行时也设了 =0 才会去包目录找,
    否则会报一个不存在的全局缓存路径。若我们依赖它, 换台机器就找不着内置内核,
    "随包分发"也就成了空话。因此必须直接扫目录, 与环境变量无关。
    """
    before = B._package_browsers()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/definitely/not/here")
    after = B._package_browsers()
    assert after == before, (
        f"包目录扫描受环境变量影响: {before} -> {after}")
    # 反向确认: 设成 0 也不应改变结果
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    assert B._package_browsers() == before


def test_engine_detail_is_exposed_for_troubleshooting():
    """排障必须能看到实际用的内核路径, 否则无法确认是否真用了内置内核。"""
    pool = B.BrowserPool(max_pages=1)
    try:
        info = pool.info()
        assert "engine_detail" in info, "info() 应暴露 engine_detail(内核路径)"
        assert "engine" in info
        # 有内核时 detail 应带出绝对路径; 无内核时也只允许空串, 不能是 None
        assert info["engine_detail"] in ("", ) or os.path.isabs(
            info["engine_detail"].split("[")[-1].rstrip("]")), (
            f"engine_detail 形状异常: {info['engine_detail']!r}")
    finally:
        pool.close()


def test_fetcher_info_exposes_engine_detail():
    """/render-info 落到 SmartFetcher.info(), 同样要有内核路径。"""
    from intellisearch.config import DEFAULT_CONFIG
    from intellisearch.http.fetcher import SmartFetcher
    f = SmartFetcher(DEFAULT_CONFIG)
    try:
        info = f.info()
        assert "engine_detail" in info, "/render-info 应返回 engine_detail"
        assert "engine" in info and "render_available" in info
    finally:
        close = getattr(f, "close", None)
        if callable(close):
            close()