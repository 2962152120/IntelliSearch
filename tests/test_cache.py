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
