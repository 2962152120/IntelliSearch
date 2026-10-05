"""示例 2：把检索结果直接作为大模型上下文（带引用编号）。

典型用法：把 context 塞进 prompt，让模型回答时标注 [1][2] 引用。
"""
from intellisearch import SearchEngine

engine = SearchEngine()

question = "铭凡 UM880 Pro 的 NPU 算力是多少 TOPS？和同价位竞品比如何？"

# 抓取正文，让模型能读到原文而不只是摘要
context = engine.search_context(
    question,
    top_k=5,
    fetch_content=True,
    max_chars=6000,
)

print(context)
print("\n" + "=" * 60)
print("把上面的文本放进 prompt 即可，例如：")
print(f"""
请基于以下检索结果回答问题，并在句末标注引用编号。
{context[:200]}...

问题：{question}
""")

engine.close()
