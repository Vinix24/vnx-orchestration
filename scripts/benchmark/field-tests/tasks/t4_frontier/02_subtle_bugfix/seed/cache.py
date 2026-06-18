"""ttl_lru_cache — a bounded cache with per-entry TTL and LRU eviction.

Used in the request layer to memoise expensive lookups. Entries expire after
`ttl` seconds; when the cache is at `capacity` a new insert evicts the
least-recently-used entry. `time_fn` is injectable for deterministic tests.
"""

from __future__ import annotations

import time as _time
from collections import OrderedDict
from typing import Any, Callable, Optional


class TTLCache:
    def __init__(self, capacity: int, ttl: float, time_fn: Optional[Callable[[], float]] = None):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self.capacity = capacity
        self.ttl = ttl
        self._time = time_fn or _time.monotonic
        # key -> (value, expires_at). OrderedDict tracks LRU order (oldest first).
        self._store: "OrderedDict[Any, tuple[Any, float]]" = OrderedDict()

    def _expired(self, expires_at: float) -> bool:
        return self._time() >= expires_at

    def get(self, key: Any) -> Optional[Any]:
        """Return the value for key, or None if absent/expired. Refreshes LRU position."""
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._expired(expires_at):
            del self._store[key]
            return None
        # mark as most-recently-used
        self._store.move_to_end(key)
        return value

    def set(self, key: Any, value: Any) -> None:
        """Insert/update key. Evicts the LRU entry when at capacity."""
        if key in self._store:
            self._store[key] = (value, self._time() + self.ttl)
            self._store.move_to_end(key)
            return

        if len(self._store) >= self.capacity:
            # At capacity: evict the least-recently-used entry to make room.
            self._store.popitem(last=False)

        self._store[key] = (value, self._time() + self.ttl)

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: Any) -> bool:
        return self.get(key) is not None

    def keys(self) -> list:
        """Return non-expired keys (LRU order, oldest first)."""
        now = self._time()
        return [k for k, (_v, exp) in self._store.items() if now < exp]
