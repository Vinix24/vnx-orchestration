"""Existing test suite for TTLCache. All pass against the shipped code."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cache import TTLCache  # noqa: E402


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_set_get_roundtrip():
    c = TTLCache(capacity=4, ttl=10, time_fn=FakeClock())
    c.set("a", 1)
    assert c.get("a") == 1


def test_get_absent_returns_none():
    c = TTLCache(capacity=4, ttl=10, time_fn=FakeClock())
    assert c.get("missing") is None


def test_ttl_expiry():
    clk = FakeClock()
    c = TTLCache(capacity=4, ttl=10, time_fn=clk)
    c.set("a", 1)
    clk.advance(11)
    assert c.get("a") is None


def test_value_present_just_before_ttl():
    clk = FakeClock()
    c = TTLCache(capacity=4, ttl=10, time_fn=clk)
    c.set("a", 1)
    clk.advance(9)
    assert c.get("a") == 1


def test_lru_eviction_all_live():
    c = TTLCache(capacity=3, ttl=100, time_fn=FakeClock())
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    c.set("d", 4)  # capacity exceeded → LRU ("a") evicted
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("d") == 4


def test_get_refreshes_lru():
    c = TTLCache(capacity=3, ttl=100, time_fn=FakeClock())
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    c.get("a")        # "a" is now most-recently-used; "b" is LRU
    c.set("d", 4)     # evicts LRU ("b"), not "a"
    assert c.get("a") == 1
    assert c.get("b") is None


def test_update_refreshes_value_and_ttl():
    clk = FakeClock()
    c = TTLCache(capacity=4, ttl=10, time_fn=clk)
    c.set("a", 1)
    clk.advance(8)
    c.set("a", 2)       # refresh
    clk.advance(8)      # 16 since first set, but only 8 since refresh
    assert c.get("a") == 2


def test_len_and_contains():
    c = TTLCache(capacity=4, ttl=100, time_fn=FakeClock())
    c.set("a", 1)
    c.set("b", 2)
    assert len(c) == 2
    assert "a" in c
    assert "z" not in c
