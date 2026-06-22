# Task t4-02 — Fix a subtle eviction bug in TTLCache (SWE-bench style)

Tier: T4 frontier. Deadline: 1 hour wallclock. You are given a WORKING module
with a passing test suite and ONE subtle bug. Fix it without breaking anything.

## The bug report (from production)

`cache.py` implements `TTLCache(capacity, ttl)` — a bounded cache with per-entry
TTL expiry and LRU eviction. Under load we see a problem:

> Freshly-cached entries disappear while stale ones survive. When the cache is
> at capacity AND it contains at least one entry that has already expired,
> inserting a new key evicts a still-valid least-recently-used entry instead of
> reclaiming the slot held by the expired entry. The expired entry then lingers
> and a live entry is lost.

Concretely: capacity=3, ttl=10. Insert a, b, c. Let `a` expire (advance time past
its TTL). Insert `d`. Expected: the expired `a` is reclaimed, and b, c, d are all
present. Actual: a live entry is evicted and `a`'s dead slot survives.

## Your task

1. Find and fix the root cause in `cache.py`. The fix must be principled — it
   should reclaim expired entries before evicting a live LRU entry.
2. Do NOT break the existing behaviour: the suite in `tests/test_cache.py`
   (basic get/set, TTL expiry, LRU eviction of live entries, LRU refresh on get,
   update semantics, len/contains) must all still pass.
3. Add a regression test to `tests/test_cache.py` that reproduces the reported
   scenario and would fail against the original buggy code.

## Rules

- Do not change the public API (`TTLCache(capacity, ttl, time_fn=...)`, `get`,
  `set`, `__len__`, `__contains__`, `keys`).
- Do not "fix" it by enlarging capacity or disabling eviction — that breaks the
  LRU contract the existing tests encode.
- No TODO comments, no stubs. Production-quality.
- Run the tests (`python -m pytest tests/ -q`) to verify before you finish.
