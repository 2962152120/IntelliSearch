#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 Chromium 内核装进 Python 包目录, 让渲染能力随包走。

背景
----
本工具的网页访问环节需要 Chromium 内核才能渲染 JS 动态页面。仅安装
playwright **库**并不会带上内核 —— 内核默认下到 `~/AppData/Local/ms-playwright/`
之类的全局缓存, 换台机器就没有了, 渲染能力随之失效。

本脚本设 `PLAYWRIGHT_BROWSERS_PATH=0`, 让 playwright 把内核装进
site-packages/playwright/driver/package/.local-browsers/ 目录。这样:

- 随 pip 包 / 虚拟环境 / Docker 镜像一起分发, 目标机器无需预装浏览器;
- 版本与本环境 playwright 严格一致, 不会再出现 "期望 chromium-1243 但只有
  chromium-1210" 这类不匹配;
- 仍然零 API Key、零外部服务。

代价: 体积 +约 150MB(解压后约 300MB), 首次安装需要下载。

用法
----
    python scripts/install_browser.py            # 装 chromium
    python scripts/install_browser.py --force    # 已有也重装
    python scripts/install_browser.py --check    # 只检查, 不安装
    python scripts/install_browser.py --launch   # 检查并真实启动一次内核

装完验证:
    python scripts/install_browser.py --check

注意: 环境变量只需在**安装时**设一次。运行时不需要 —— 项目里的
find_chromium() 会直接扫包目录定位内核, 不依赖该变量。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

# 包内内核目录名(playwright 布局: playwright/driver/package/<此目录>)
BUNDLED_DIRNAME = ".local-browsers"

# 官方 CDN 不通/不稳时的回退镜像(阿里 npmmirror, 实测可达且支持断点续传)。
# 国内网络下官方 cdn.playwright.dev 常在长传输中被掐断, 表现为
# "Download failed: server closed connection"。
MIRROR_HOST = "https://cdn.npmmirror.com/binaries/playwright"

# 内核可执行文件名(按平台)
_EXE_NAMES = {
    "win32": ("chrome.exe", "headless_shell.exe"),
    "darwin": ("chrome", "headless_shell", "Chromium"),
    "linux": ("chrome", "headless_shell"),
}.get(sys.platform, ("chrome", "headless_shell"))


def _env() -> dict:
    """让 playwright 把内核装进包目录而非全局缓存。"""
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    return env


def package_root() -> pathlib.Path:
    """site-packages/playwright 目录; 未安装返回空 Path。"""
    try:
        import playwright
    except ImportError:
        return pathlib.Path()
    return pathlib.Path(playwright.__file__).resolve().parent


def bundled_dir() -> str:
    """包内内核目录(playwright 1.40+ 布局)。playwright 未安装时返回 ""。"""
    root = package_root()
    if not root.parts:
        return ""
    return str(root / "driver" / "package" / BUNDLED_DIRNAME)


def find_binaries(base: str) -> list:
    """在包内内核目录里找出全部可执行文件。"""
    if not base or not os.path.isdir(base):
        return []
    hits = []
    for root, _dirs, files in os.walk(base):
        for fn in files:
            if fn in _EXE_NAMES:
                hits.append(os.path.join(root, fn))
    return sorted(hits)


def check() -> int:
    """检查包内内核是否可用。返回进程退出码。"""
    print("playwright   :", end=" ")
    try:
        import playwright
        print("已安装", getattr(playwright, "__version__", "(版本未知)"))
    except ImportError:
        print("未安装 —— 请先 pip install 'intellisearch[render]'")
        return 1

    d = bundled_dir()
    print("包内内核目录 :", d or "(无法确定)")
    if not os.path.isdir(d):
        print("状态         : 未安装 —— 运行 python scripts/install_browser.py")
        return 1

    hits = find_binaries(d)
    if not hits:
        print("状态         : 目录存在但找不到可执行文件, 建议 --force 重装")
        return 1

    size = sum(os.path.getsize(h) for h in hits)
    print(f"状态         : 已就绪 (可执行文件 {len(hits)} 个)")
    for h in hits[:4]:
        print("               ", h)
    if size:
        print(f"               合计 {size / 1048576:.1f} MB")
    return 0


def verify_launch() -> int:
    """真实启动一次内核, 确认它能跑起来。"""
    hits = find_binaries(bundled_dir())
    if not hits:
        print("无可执行文件, 跳过启动验证")
        return 1
    print("正在真实启动内核验证(约 10 秒)...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright 未安装")
        return 1
    exe = hits[0]
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, executable_path=exe,
                                  timeout=30000,
                                  args=["--no-sandbox", "--disable-gpu"])
            pg = b.new_page()
            pg.set_content("<h1 id=t>ok</h1>")
            got = pg.inner_text("#t")
            b.close()
    except Exception as e:                                # noqa: BLE001
        print(f"启动失败: {type(e).__name__}: {str(e)[:300]}")
        return 1
    print(f"启动成功, 页面文本 = {got!r}")
    return 0 if got == "ok" else 1


def _run_install(host: str = "") -> int:
    env = _env()
    if host:
        env["PLAYWRIGHT_DOWNLOAD_HOST"] = host
    r = subprocess.run([sys.executable, "-m", "playwright", "install",
                        "chromium"], env=env)
    return r.returncode


def install(force: bool = False, mirror: bool = False) -> int:
    if not force:
        rc = check()
        if rc == 0:
            print("\n内核已存在, 无需重装(要重装请加 --force)")
            return 0
    print("正在下载 Chromium 内核(约 150MB, 请耐心等待)...\n")

    hosts = [MIRROR_HOST] if mirror else ["", MIRROR_HOST]
    for i, host in enumerate(hosts, 1):
        label = host or "官方 CDN"
        print(f"[{i}/{len(hosts)}] 经 {label} 下载 ...")
        rc = _run_install(host)
        if rc == 0:
            print()
            return check()
        if i < len(hosts):
            print(f"  -> {label} 失败, 换下一个源重试(已下载部分会续传)\n")

    print("\n全部下载源均失败。可手动重试:\n"
          "  PLAYWRIGHT_BROWSERS_PATH=0 python -m playwright install chromium\n"
          f"  PLAYWRIGHT_BROWSERS_PATH=0 PLAYWRIGHT_DOWNLOAD_HOST={MIRROR_HOST} \\\n"
          "      python -m playwright install chromium\n"
          "或直接下载 zip 解压到:\n"
          f"  {bundled_dir()}/chromium-<版本号>/")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="安装/检查内置 Chromium 内核")
    ap.add_argument("--check", action="store_true", help="只检查, 不安装")
    ap.add_argument("--force", action="store_true", help="已有也重新安装")
    ap.add_argument("--launch", action="store_true", help="检查并真实启动一次内核")
    ap.add_argument("--mirror", action="store_true",
                    help="直接用 npmmirror 镜像下载(官方 CDN 不通时)")
    a = ap.parse_args()

    if a.launch:
        rc = check()
        return verify_launch() if rc == 0 else rc
    if a.check:
        return check()
    return install(force=a.force, mirror=a.mirror)


if __name__ == "__main__":
    sys.exit(main())
