"""示例 1：最简用法 —— 检索并拿到结构化结果。"""
from intellisearch import SearchEngine

engine = SearchEngine()

resp = engine.search("铭凡 UM880 Pro 的 NPU 算力多少", top_k=5)

print(f"状态: {resp.status.value}  说明: {resp.message}")
print(f"改写后的检索词: {resp.parsed_query.effective}")
print()

for i, r in enumerate(resp.results, 1):
    print(f"[{i}] {r.title}")
    print(f"    {r.url}")
    print(f"    时间: {r.publish_time or '未知'} | 类型: {r.source_type.value} "
          f"| 相关性: {r.relevance_score} | 可信度: {r.credibility}")
    print(f"    {r.snippet[:120]}")
    print()

# 各检索源的执行情况（排障用）
for p in resp.providers:
    print(f"  源 {p.name}: ok={p.ok} 状态={p.status} 条数={p.count} 耗时={p.elapsed_ms}ms")

engine.close()
