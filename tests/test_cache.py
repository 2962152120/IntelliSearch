"""缓存测试。"""
import time

from intellisearch.cache import Cache


def test_set_get_roundtrip(tmp_path):
    c = Cache(path=str(tmp_path / "c.db"), ttl=60, enabled=True)
    c.set("k1", {"a": 1}, "search")
    assert c.get("k1", "search") == {"a": 1}
    c.close()


def test_expired_entry_returns_none(tmp_path):
    c = Cache(path=str(tmp_path / "c.db"), ttl=1, enabled=True)
    c.set("k", {"a": 1}, "search", ttl=1)
    time.sleep(1.2)
    assert c.get("k", "search") is None
    c.close()


def test_namespace_isolation(tmp_path):
    c = Cache(path=str(tmp_path / "c.db"), enabled=True)
    c.set("k", "search-value", "search")
    c.set("k", "page-value", "page")
    assert c.get("k", "search") == "search-value"
    assert c.get("k", "page") == "page-value"
    c.close()


def test_disabled_cache_is_noop(tmp_path):
    c = Cache(path=str(tmp_path / "c.db"), enabled=False)
    c.set("k", {"a": 1})
    assert c.get("k") is None
    assert c.info()["enabled"] is False


def test_eviction_respects_max_entries(tmp_path):
    c = Cache(path=str(tmp_path / "c.db"), ttl=600, max_entries=5, enabled=True)
    for i in range(50):
        c.set(f"k{i}", i, "search")
    assert c.info()["entries"] <= 5
    c.close()


def test_purge(tmp_path):
    c = Cache(path=str(tmp_path / "c.db"), enabled=True)
    for i in range(5):
        c.set(f"k{i}", i, "search")
    assert c.purge("search") == 5
    assert c.info()["entries"] == 0
    c.close()


def test_corrupted_payload_does_not_raise(tmp_path):
    c = Cache(path=str(tmp_path / "c.db"), enabled=True)
    c._conn.execute("INSERT OR REPLACE INTO cache VALUES ('bad','search',"
                    "'not-json',0,?,0)", (time.time() + 100,))
    c._conn.commit()
    assert c.get("bad", "search") is None
    c.close()


def test_hit_counter_increments(tmp_path):
    c = Cache(path=str(tmp_path / "c.db"), enabled=True)
    c.set("k", 1, "search")
    c.get("k", "search")
    c.get("k", "search")
    assert c.stats["hits"] == 2
    c.close()


def test_in_memory_cache_works():
    c = Cache(path=":memory:", enabled=True)
    c.set("k", [1, 2, 3], "search")
    assert c.get("k", "search") == [1, 2, 3]
    c.close()


def test_eviction_does_not_delete_same_key_in_other_namespace(tmp_path):
    """LRU 淘汰只能删被选中的那一行, 不能连带删除其它 namespace 的同名 key。

    search / page 两个 namespace 都使用同形态的 key(哈希串), 旧实现按
    `WHERE key IN (SELECT key ...)` 删除, 同名 key 的所有行一起被删 ——
    这里 page 行被淘汰后, search 行也会被误删。必须按 rowid 精确删除。
    """
    c = Cache(path=str(tmp_path / "c.db"), ttl=600, max_entries=1, enabled=True)
    c.set("k", "page-value", "page")
    time.sleep(0.01)                      # 保证 created_at 有先后, 淘汰 page 行
    c.set("k", "search-value", "search")  # 触发淘汰 1 行
    assert c.get("k", "search") == "search-value", "search namespace 的行被误删了"
    assert c.get("k", "page") is None
    assert c.info()["entries"] == 1
    c.close()
