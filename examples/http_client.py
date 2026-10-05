"""示例 4：调用 HTTP 服务（先运行 isearch --serve 8787）。"""
import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8787"


def search(query, **params):
    qs = urllib.parse.urlencode({"q": query, **params})
    with urllib.request.urlopen(f"{BASE}/search?{qs}", timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def search_context(query, max_chars=4000):
    qs = urllib.parse.urlencode({"q": query, "format": "context",
                                 "max_chars": max_chars, "top_k": 5})
    with urllib.request.urlopen(f"{BASE}/search?{qs}", timeout=30) as r:
        return r.read().decode("utf-8")


if __name__ == "__main__":
    print("--- 健康检查 ---")
    with urllib.request.urlopen(f"{BASE}/health", timeout=10) as r:
        print(r.read().decode())

    print("\n--- JSON 检索 ---")
    d = search("AMD Zen5 架构", top_k=3)
    print(f"状态: {d['status']}  返回 {d['returned']} 条")
    for x in d["results"]:
        print(f"  - {x['title'][:50]}")

    print("\n--- 上下文文本（可直接喂给模型） ---")
    print(search_context("Python 列表推导式")[:600])
