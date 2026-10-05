"""示例 3：多轮会话 —— 同一对话内的指代消解。

第一轮问"铭凡 UM880 Pro 的 NPU 算力"，第二轮问"它的价格"，
第二轮会自动关联上文的实体，无需重复完整名称。
"""
from intellisearch import SearchEngine
from intellisearch.models import QueryOptions

engine = SearchEngine()
sid = "demo-session-001"
opts = QueryOptions(top_k=3, session_id=sid)

for q in ["铭凡 UM880 Pro 的 NPU 算力多少", "它的价格呢", "散热表现怎么样"]:
    resp = engine.search(q, opts)
    print(f"\n>>> 提问: {q}")
    print(f"    实际检索: {resp.parsed_query.effective}")
    print(f"    状态: {resp.status.value}，{len(resp.results)} 条")
    for r in resp.results[:2]:
        print(f"      - {r.title[:50]}  ({r.domain})")

engine.close()
