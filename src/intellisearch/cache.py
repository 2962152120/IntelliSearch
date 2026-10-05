"""结果缓存(SQLite)。

缓存什么:
1. 检索结果(query 维度) —— 主要收益来源, 命中即零网络开销;
2. 页面正文(url 维度, TTL 更长) —— 抓取是最贵的操作。

特性: TTL 过期、LRU 容量淘汰、损坏记录自动丢弃、线程安全。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT NOT NULL,
    namespace  TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    hits       INTEGER DEFAULT 0,
    PRIMARY KEY (namespace, key)
);
CREATE INDEX IF NOT EXISTS idx_ns_exp ON cache(namespace, expires_at);
CREATE INDEX IF NOT EXISTS idx_created ON cache(created_at);
"""


class Cache:
    """SQLite 缓存。enabled=False 时全部操作为空操作。"""

    def __init__(self, path: str = ":memory:", ttl: int = 1800,
                 max_entries: int = 5000, enabled: bool = True):
        self.path = path
        self.ttl = ttl
        self.max_entries = max_entries
        self.enabled = enabled
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self.stats = {"hits": 0, "misses": 0, "sets": 0, "errors": 0}
        if enabled:
            self._init_db()

    def _init_db(self):
        try:
            if self.path != ":memory:":
                d = os.path.dirname(os.path.abspath(self.path))
                os.makedirs(d, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=5,
                                         check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except Exception:              # noqa: BLE001 - 缓存不可用不应影响主流程
            self._conn = None
            self.enabled = False

    # ---------- 基础操作 ----------
    def get(self, key: str, namespace: str = "search") -> Optional[Any]:
        if not self.enabled or not self._conn:
            return None
        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT payload, expires_at FROM cache WHERE key=? AND namespace=?",
                    (key, namespace))
                row = cur.fetchone()
            if not row:
                self.stats["misses"] += 1
                return None
            payload, exp = row
            if exp and exp < time.time():
                self.delete(key, namespace)
                self.stats["misses"] += 1
                return None
            with self._lock:
                self._conn.execute(
                    "UPDATE cache SET hits = hits + 1 WHERE key=? AND namespace=?",
                    (key, namespace))
                self._conn.commit()
            self.stats["hits"] += 1
            return json.loads(payload)
        except Exception:              # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["misses"] += 1
            return None

    def set(self, key: str, value: Any, namespace: str = "search",
            ttl: int = None) -> None:
        if not self.enabled or not self._conn:
            return
        ttl = self.ttl if ttl is None else ttl
        try:
            payload = json.dumps(value, ensure_ascii=False)
            now = time.time()
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO cache (key, namespace, payload, "
                    "created_at, expires_at, hits) VALUES (?,?,?,?,?,0)",
                    (key, namespace, payload, now, now + ttl))
                self._conn.commit()
                self._evict()
            self.stats["sets"] += 1
        except Exception:              # noqa: BLE001
            self.stats["errors"] += 1

    def delete(self, key: str, namespace: str = "search") -> None:
        if not self.enabled or not self._conn:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM cache WHERE key=? AND namespace=?", (key, namespace))
                self._conn.commit()
        except Exception:              # noqa: BLE001
            pass

    def _evict(self):
        """超容量时按 (过期优先, 其次最久未命中) 淘汰。"""
        try:
            cur = self._conn.execute("SELECT COUNT(*) FROM cache")
            n = cur.fetchone()[0]
            if n <= self.max_entries:
                return
            self._conn.execute("DELETE FROM cache WHERE expires_at < ?",
                               (time.time(),))
            cur = self._conn.execute("SELECT COUNT(*) FROM cache")
            n = cur.fetchone()[0]
            if n > self.max_entries:
                # 按 LRU 近似: hits 最少 + 创建最早
                self._conn.execute(
                    "DELETE FROM cache WHERE key IN ("
                    "  SELECT key FROM cache ORDER BY hits ASC, created_at ASC"
                    "  LIMIT ?)", (n - self.max_entries,))
            self._conn.commit()
        except Exception:              # noqa: BLE001
            pass

    def purge(self, namespace: str = None) -> int:
        if not self._conn:
            return 0
        try:
            with self._lock:
                if namespace:
                    cur = self._conn.execute(
                        "DELETE FROM cache WHERE namespace=?", (namespace,))
                else:
                    cur = self._conn.execute("DELETE FROM cache")
                self._conn.commit()
                return cur.rowcount or 0
        except Exception:              # noqa: BLE001
            return 0

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:          # noqa: BLE001
                pass
            self._conn = None

    def info(self) -> Dict[str, Any]:
        n = 0
        if self._conn:
            try:
                n = self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            except Exception:          # noqa: BLE001
                n = 0
        return {"enabled": self.enabled, "path": self.path, "entries": n,
                **self.stats}
