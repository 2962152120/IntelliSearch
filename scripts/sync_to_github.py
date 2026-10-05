#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把本地工作区同步到 GitHub(Git Data API)。

为什么不用 `git push`
---------------------
本机到 github.com 的 git 传输通道(schannel / CONNECT 隧道)不稳定,
`git ls-remote` 时好时坏; 而 `gh` CLI 的 HTTPS 调用稳定可用。
故走 Git Data API: blob -> tree -> commit -> 更新 ref。

用法
----
    python scripts/sync_to_github.py --dry-run   # 只看差异, 不上传
    python scripts/sync_to_github.py -m "feat: ..."   # 上传并提交
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = "2962152120/IntelliSearch"   # 可用 --repo 覆盖(fork 后改这里)
BRANCH = "main"
ROOT = pathlib.Path(__file__).resolve().parents[1]

# 不随仓库发布的内容
SKIP_DIRS = {".git", ".workbuddy", "__pycache__", ".pytest_cache",
             ".mypy_cache", ".ruff_cache", "node_modules", "dist", "build"}
SKIP_SUFFIX = {".pyc", ".pyo", ".sqlite3", ".sqlite", ".log"}


def gh(args, payload=None):
    """调用 gh api。JSON 一律走 --input 文件(stdin 会 404)。"""
    cmd = ["gh", "api"] + args
    tmp = None
    if payload is not None:
        fd, tmp = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        cmd += ["--input", tmp]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise RuntimeError(f"gh api {' '.join(args)} 失败:\n"
                               f"{r.stdout}\n{r.stderr}")
        return json.loads(r.stdout) if r.stdout.strip() else {}
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def local_files():
    """本地待同步文件(相对路径 -> 绝对路径), 已排除忽略项。"""
    out = {}
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(ROOT).parts
        if any(seg in SKIP_DIRS for seg in rel_parts):
            continue
        if p.suffix in SKIP_SUFFIX:
            continue
        rel = "/".join(rel_parts)
        # .gitignore 里的硬规则
        if rel.startswith(".env") and rel != ".env.example":
            continue
        out[rel] = p
    return out


def blob_sha1(data: bytes) -> str:
    """git blob SHA-1 —— 与 GitHub 一致, 可在本地算出, 无需上传即可比对。

    形式: sha1("blob <长度>\\0" + 内容)。
    """
    import hashlib
    h = hashlib.sha1()
    h.update(b"blob " + str(len(data)).encode("ascii") + b"\0")
    h.update(data)
    return h.hexdigest()


def remote_tree(commit_sha):
    """远端 commit 的递归树: {path: sha} (仅 blob)。"""
    t = gh([f"repos/{REPO}/git/trees/{commit_sha}?recursive=1"])
    return {e["path"]: e["sha"] for e in t.get("tree", [])
            if e["type"] == "blob"}


def make_blob(data: bytes):
    r = gh([f"repos/{REPO}/git/blobs"], {
        "content": base64.b64encode(data).decode("ascii"),
        "encoding": "base64",
    })
    return r["sha"]


def main() -> int:
    global REPO, BRANCH
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", default="chore: sync from local workspace")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repo", default=REPO,
                    help="owner/name, fork 后用它指向自己的仓库")
    ap.add_argument("--branch", default=BRANCH)
    a = ap.parse_args()
    REPO, BRANCH = a.repo, a.branch

    # REST 的 commits/{ref}: tree 在 commit.tree.sha 下, 不是顶层 tree
    head = gh([f"repos/{REPO}/commits/{BRANCH}"])
    head_sha = head["sha"]
    head_tree = head["commit"]["tree"]["sha"]
    print(f"远端 {BRANCH} = {head_sha[:8]}  "
          f"{head['commit']['message'].splitlines()[0]}")

    local = local_files()
    remote = remote_tree(head_sha)
    print(f"本地 {len(local)} 个文件 / 远端 {len(remote)} 个 blob")

    # 先在本地算 SHA-1, 只对真正变化的文件建 blob
    contents = {rel: p.read_bytes() for rel, p in local.items()}
    shas = {rel: blob_sha1(data) for rel, data in contents.items()}
    changed = sorted(rel for rel in local if remote.get(rel) != shas[rel])
    deleted = sorted(rel for rel in remote if rel not in local)

    print(f"\n新增/改动 {len(changed)}, 删除 {len(deleted)}")
    for rel in changed:
        old = remote.get(rel)
        print(f"  {'新增' if not old else '改动'}  {rel}")
    for rel in deleted:
        print(f"  删除  {rel}")

    if a.dry_run:
        print("\n(dry-run, 未上传)")
        return 0
    if not changed and not deleted:
        print("\n无变化, 跳过")
        return 0

    # 变化的文件才真正建 blob; 内容相同的直接复用远端 sha
    print("\n上传 blob ...")
    tree_entries = []
    for rel in changed:
        sha = make_blob(contents[rel])
        assert sha == shas[rel], f"{rel}: 远端 blob sha 与本地不一致"
        tree_entries.append({"path": rel, "mode": "100644",
                             "type": "blob", "sha": sha})
        print(f"  {rel} -> {sha[:10]}")
    tree_entries += [{"path": rel, "sha": None} for rel in deleted]

    tree = gh([f"repos/{REPO}/git/trees"],
              {"base_tree": head_tree, "tree": tree_entries})
    print(f"\n新 tree = {tree['sha'][:12]}")

    commit = gh([f"repos/{REPO}/git/commits"], {
        "message": a.message,
        "tree": tree["sha"],
        "parents": [head_sha],
    })
    print(f"新 commit = {commit['sha'][:12]}")

    gh([f"repos/{REPO}/git/refs/heads/{BRANCH}"],
       {"sha": commit["sha"], "force": False})
    print(f"\n已推送到 {BRANCH}: {commit['sha']}")
    print(f"https://github.com/{REPO}/commit/{commit['sha']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                                # noqa: BLE001
        print(f"失败: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
