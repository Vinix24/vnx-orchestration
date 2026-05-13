#!/usr/bin/env python3
"""Shared cache helper for ADR / operator-memory / schema-section indexers.

Fixes two recurring bugs found in W5.1, W5.3, W5.4 codex rounds:

1. CACHE SINGLETON IGNORES dir_path: caller passes adr_dir=A on first call,
   then adr_dir=B on second call within 60s — got cached A's index.
   FIX: key cache by resolved dir path; different dirs = different entries.

2. DELETION DETECTION BROKEN: _mtime_changed() iterates only currently-existing
   files; deleted files never invalidate. Cached entry references a deleted source.
   FIX: also track filename SET; refresh if set differs.

Usage:
    cache = IndexCache(ttl_sec=60, scanner=_scan_my_dir)
    entries = cache.get(my_dir)  # auto-refreshes if needed
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class _CacheEntry(Generic[T]):
    dir_path: Path
    entries: T
    file_set: frozenset       # str paths present at scan time (detects deletions)
    file_mtimes: dict         # {str(path): mtime} for modification detection
    loaded_at: float


class IndexCache(Generic[T]):
    """TTL + mtime + file-set invalidating cache, keyed by resolved directory path.

    The scanner callable signature: (dir_path: Path | None) -> (T, dict[str, float])
    where the dict maps str(file_path) -> mtime for every file the scanner touched.
    IndexCache derives file_set from dict keys and handles all stale detection.
    """

    def __init__(
        self,
        ttl_sec: float,
        scanner: Callable,
        glob_pattern: str = "*.md",
    ) -> None:
        self._ttl = ttl_sec
        self._scanner = scanner
        self._glob = glob_pattern
        self._cache: dict[str, _CacheEntry[T]] = {}

    def get(self, dir_path: Optional[Path]) -> T:
        """Return cached entries for dir_path; refresh if stale.

        Returns scanner's empty result if dir_path is None or not a directory.
        None and missing dirs are NOT cached (scanner is cheap for these cases).
        """
        if dir_path is None:
            entries, _ = self._scanner(None)
            return entries

        resolved = Path(dir_path).resolve()

        if not resolved.is_dir():
            entries, _ = self._scanner(resolved)
            return entries

        key = str(resolved)
        entry = self._cache.get(key)
        if entry is None or self._is_stale(entry):
            entries, mtimes = self._scanner(resolved)
            self._cache[key] = _CacheEntry(
                dir_path=resolved,
                entries=entries,
                file_set=frozenset(mtimes.keys()),
                file_mtimes=dict(mtimes),
                loaded_at=time.time(),
            )

        return self._cache[key].entries

    def invalidate(self, dir_path: Optional[Path] = None) -> None:
        """Force refresh on next get(). Pass None to clear all cached dirs."""
        if dir_path is None:
            self._cache.clear()
        else:
            key = str(Path(dir_path).resolve())
            self._cache.pop(key, None)

    def _is_stale(self, entry: _CacheEntry[T]) -> bool:
        """True if TTL expired OR file set changed OR any mtime changed."""
        if (time.time() - entry.loaded_at) > self._ttl:
            return True

        if not entry.dir_path.is_dir():
            return True

        # Re-glob to get current file set — detects both additions AND deletions
        try:
            current_files = frozenset(
                str(f) for f in entry.dir_path.glob(self._glob)
            )
        except OSError:
            return True

        if current_files != entry.file_set:
            return True

        # Check mtime changes for files present in both old and new sets
        for filepath_str, stored_mtime in entry.file_mtimes.items():
            try:
                mtime = Path(filepath_str).stat().st_mtime
            except OSError:
                return True
            if mtime != stored_mtime:
                return True

        return False
