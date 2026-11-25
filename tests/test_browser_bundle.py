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


# 系统浏览器候选的两种 source: Windows 分支叫 "system", Linux/macOS 分支叫 "which"。
_SYSTEM_SOURCES = ("system", "which")

# 假系统浏览器路径: Windows 分支用 os.path.isfile 判定, Linux/macOS 用 shutil.which。
_FAKE_WIN_SYSTEM = os.path.join("/fake/root", "chrome.exe")
_FAKE_WHICH_SYSTEM = "/fake/bin/google-chrome"


def _stub_system_browser(monkeypatch):
    """让"系统浏览器"候选在任意平台都能被确定地构造出来。

    _candidates() 的兜底分支按平台分叉: win32 走 os.path.isfile,
    linux 走 shutil.which, macOS 先 os.path.exists(_MAC_APPS) 再 shutil.which。
    这里把两条探测方式同时替换成只认本文件假路径的版本(_MAC_APPS 置空,
    让 macOS 也走 shutil.which), 于是 ubuntu / windows / macos 的 CI 都能
    拿到一个系统候选, source 可能是 "system", 也可能是 "which"。

    这是防假绿的关键: 若 stub 不生效、系统候选没造出来, 顺序断言就无从比较,
    测试必须直接变红(见 _assert_system_candidate), 而不是像旧版那样用
    `if "system" in srcs:` 静默跳过。
    """
    monkeypatch.setattr(B, "_WIN_PAIRS",
                        [("FakeChrome", "chrome", "chrome.exe")])
    monkeypatch.setattr(B, "_WIN_ROOTS", ["/fake/root"])
    monkeypatch.setattr(B, "_MAC_APPS", [])
    monkeypatch.setattr(B.os.path, "isfile",
                        lambda p: p == _FAKE_WIN_SYSTEM)
    monkeypatch.setattr(B.shutil, "which",
                        lambda exe: _FAKE_WHICH_SYSTEM)


def _assert_system_candidate(srcs):
    """返回命中的系统浏览器 source, 并硬性要求它必须存在。

    旧版测试用 `if "system" in srcs:` 包住断言 —— 先把 os.path.isfile 恒设为
    False, 系统候选永远构造不出来, 断言体整段不执行, 测试永远绿。这里改成
    显式硬断言: 系统候选不存在就直接失败, 顺序断言才有意义。
    """
    hits = [s for s in srcs if s in _SYSTEM_SOURCES]
    assert hits, (
        "这里必须真的造出 system 候选, 否则顺序断言无效(假绿): "
        f"实际候选来源 {srcs}")
    return hits[0]


def test_bundled_chromium_is_preferred_over_system_browser(monkeypatch):
    """包内自带内核必须排在系统浏览器之前 —— 这是"换机器也能跑"的前提。"""
    monkeypatch.delenv("IS_BROWSER_PATH", raising=False)
    monkeypatch.delenv("IS_CHROME_PATH", raising=False)
    monkeypatch.setattr(B, "_playwright_bundled",
                        lambda: "/pkg/.local-browsers/chromium-1243/chrome.exe")
    monkeypatch.setattr(B, "_playwright_browsers",
                        lambda: ["/cache/chromium-1210/chrome.exe"])
    _stub_system_browser(monkeypatch)

    srcs = [s for s, _ in _sources()]
    assert "playwright-bundled" in srcs, f"缺少包内内核候选: {srcs}"
    assert "ms-playwright-cache" in srcs, f"缺少缓存内核候选: {srcs}"
    # 防假绿: 必须先用硬断言确认系统候选真的存在(不存在会在这里失败),
    # 否则下面的 index 断言可能因比较对象缺失而失效。
    system_src = _assert_system_candidate(srcs)
    assert srcs.index("playwright-bundled") < srcs.index(system_src), (
        f"包内内核必须优先于系统浏览器, 实际顺序: {srcs}")
    # 原语义: bundled 同样优先于 ms-playwright 缓存
    assert srcs.index("playwright-bundled") < srcs.index("ms-playwright-cache"), (
        f"包内内核必须优先于缓存内核, 实际顺序: {srcs}")


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
    _stub_system_browser(monkeypatch)

    srcs = [s for s, _ in _sources()]
    assert "ms-playwright-cache" in srcs, f"缺少缓存内核候选: {srcs}"
    # 防假绿: 硬断言系统候选必须存在, 否则下面的顺序断言没有比较对象。
    system_src = _assert_system_candidate(srcs)
    assert srcs.index("ms-playwright-cache") < srcs.index(system_src), (
        f"缓存内核必须优先于系统浏览器, 实际顺序: {srcs}")


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
    """语法校验用内存 compile, 不落 __pycache__。

    原实现用 py_compile, 会往 scripts/__pycache__ 写 .pyc —— 在只读检出、
    受限沙箱或缓存目录无写权限的环境里会误报 PermissionError, 与"脚本语法
    是否正确"无关(实测本机就被旧权限残留卡了一个失败)。compile() 语义等价
    (同样会抛 SyntaxError), 且零文件系统副作用。
    """
    p = _script()
    compile(p.read_text(encoding="utf-8"), str(p), "exec")


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


def test_package_browsers_matches_new_headless_layout(tmp_path, monkeypatch):
    """回归: playwright 1.5x 的 headless shell 目录/文件名都带连字符。

    旧 glob 只认 chromium_headless_shell-*/chrome-win64/headless_shell.exe,
    实际布局是 chromium_headless_shell-*/chrome-headless-shell-win64/
    chrome-headless-shell.exe —— 漏掉后"只有 headless 内核"的精简部署会
    被误判为无内置内核。用一个伪造的包目录验证两种布局都能被发现。
    """
    import types

    # 伪造 site-packages/playwright/driver/package/.local-browsers 布局
    pkg = tmp_path / "playwright"
    pkg.mkdir()
    mod = types.ModuleType("playwright")
    mod.__file__ = str(pkg / "__init__.py")
    lb = pkg / "driver" / "package" / ".local-browsers"
    # 新布局(win)
    new = lb / "chromium_headless_shell-1255" / "chrome-headless-shell-win64"
    new.mkdir(parents=True)
    (new / "chrome-headless-shell.exe").write_bytes(b"x")
    # 新布局(linux)
    newl = lb / "chromium_headless_shell-1255" / "chrome-linux"
    newl.mkdir(parents=True)
    (newl / "chrome-headless-shell").write_bytes(b"x")
    # 旧布局(win)
    old = lb / "chromium_headless_shell-1210" / "chrome-win64"
    old.mkdir(parents=True)
    (old / "headless_shell.exe").write_bytes(b"x")

    monkeypatch.setitem(sys.modules, "playwright", mod)
    found = B._package_browsers()
    names = [os.path.basename(p) for p in found]
    assert "chrome-headless-shell.exe" in names, f"漏掉新版 win headless: {found}"
    assert "chrome-headless-shell" in names, f"漏掉新版 linux headless: {found}"
    assert "headless_shell.exe" in names, f"漏掉旧版 win headless: {found}"


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