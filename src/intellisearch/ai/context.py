"""会话维度的检索上下文记忆(需求四.4)。

同一个对话/会话内, 记住上一轮检索的主题与实体, 后续 query 自动关联上下文。

例::

    session: "铭凡 UM880 Pro 的 NPU 算力多少"
    下一轮:  "它的价格呢"
    -> 自动补全为 "铭凡 UM880 Pro 价格"

纯内存实现(进程内 LRU + TTL); 多实例部署时可通过 SessionStore 接口换成 Redis。
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

# 指代词: 命中说明当前 query 依赖上文
REFERENCE_WORDS = {
    "它", "这款", "这个", "那款", "那个", "这货", "其", "同上", "这款产品",
    "it", "this", "that", "the same", "its",
}


@dataclass
class SessionState:
    session_id: str
    queries: List[str] = field(default_factory=list)
    entities: "OrderedDict[str, float]" = field(default_factory=OrderedDict)
    urls_seen: List[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def touch(self):
        self.updated_at = time.time()


class SessionStore:
    """带 TTL 与容量上限的会话存储(线程安全)。"""

    def __init__(self, ttl: float = 1800.0, max_sessions: int = 200,
                 max_entities: int = 12):
        self.ttl = ttl
        self.max_sessions = max_sessions
        self.max_entities = max_entities
        self._data: "OrderedDict[str, SessionState]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, session_id: str, create: bool = True) -> Optional[SessionState]:
        with self._lock:
            self._evict()
            st = self._data.get(session_id)
            if st is None and create:
                st = SessionState(session_id=session_id)
                self._data[session_id] = st
            if st:
                self._data.move_to_end(session_id)
                st.touch()
            return st

    def _evict(self):
        now = time.time()
        for k in [k for k, v in self._data.items() if now - v.updated_at > self.ttl]:
            self._data.pop(k, None)
        while len(self._data) > self.max_sessions:
            self._data.popitem(last=False)

    def remember(self, session_id: str, query: str, entities: Sequence[str] = (),
                 urls: Sequence[str] = ()) -> None:
        st = self.get(session_id)
        if not st:
            return
        with self._lock:
            st.queries.append(query)
            st.queries = st.queries[-10:]
            for e in entities or []:
                e = (e or "").strip()
                if 1 < len(e) <= 24:
                    st.entities[e] = time.time()
                    st.entities.move_to_end(e)
            st.entities = OrderedDict(list(st.entities.items())[-self.max_entities:])
            for u in urls or []:
                if u and u not in st.urls_seen:
                    st.urls_seen.append(u)
            st.urls_seen = st.urls_seen[-50:]
            st.touch()

    def entities(self, session_id: str) -> List[str]:
        st = self.get(session_id, create=False)
        return list(st.entities.keys()) if st else []

    def last_query(self, session_id: str) -> str:
        st = self.get(session_id, create=False)
        return st.queries[-1] if st and st.queries else ""

    def clear(self, session_id: str = None) -> None:
        with self._lock:
            if session_id:
                self._data.pop(session_id, None)
            else:
                self._data.clear()

    def __len__(self):
        return len(self._data)


def needs_context(query: str) -> bool:
    """判断 query 是否依赖上文(含有指代词或过短)。"""
    q = (query or "").strip()
    if not q:
        return False
    if len(q) <= 6:
        return True
    return any(w in q for w in REFERENCE_WORDS)


def apply_context(query: str, session_id: str,
                  store: SessionStore) -> str:
    """把会话中的实体关联到当前 query。

    仅在 query 明显依赖上文时才拼接, 避免污染正常查询。
    """
    if not session_id or not store or not needs_context(query):
        return query
    ents = store.entities(session_id)
    if not ents:
        return query
    # 取最近 2 个实体
    picked = [e for e in ents[-2:] if e.lower() not in query.lower()]
    if not picked:
        return query
    return f"{' '.join(picked)} {query}".strip()
