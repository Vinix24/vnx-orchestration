#!/usr/bin/env python3
"""Tests for index_cache.py (CFX-W5-2).

Covers:
  - TTL-based invalidation
  - Mtime-based invalidation
  - File-set change invalidation (additions AND deletions)
  - Cache key isolation by dir path
  - None/missing dir handling
  - IndexCache.invalidate()
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from index_cache import IndexCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scanner(values_by_dir: dict):
    """Return a scanner that returns canned (entries, mtimes) per dir_path key."""
    calls: list = []

    def scanner(dir_path):
        calls.append(dir_path)
        key = str(dir_path) if dir_path is not None else None
        return values_by_dir.get(key, ([], {}))

    scanner.calls = calls
    return scanner


def _write_file(path: Path, content: str = "x") -> Path:
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# TTL invalidation
# ---------------------------------------------------------------------------

class TestTtlInvalidation:
    def test_returns_cached_value_within_ttl(self, tmp_path):
        calls = []

        def scanner(d):
            calls.append(d)
            return (["v1"], {})

        cache = IndexCache(ttl_sec=60, scanner=scanner, glob_pattern="*.md")
        cache.get(tmp_path)
        cache.get(tmp_path)
        assert len(calls) == 1, "should only scan once within TTL"

    def test_refreshes_after_ttl(self, tmp_path):
        calls = []

        def scanner(d):
            calls.append(d)
            return (["v1"], {})

        cache = IndexCache(ttl_sec=0.01, scanner=scanner, glob_pattern="*.md")
        cache.get(tmp_path)
        time.sleep(0.05)
        cache.get(tmp_path)
        assert len(calls) == 2, "should re-scan after TTL expires"


# ---------------------------------------------------------------------------
# Mtime invalidation
# ---------------------------------------------------------------------------

class TestMtimeInvalidation:
    def test_refreshes_when_file_mtime_changes(self, tmp_path):
        f = _write_file(tmp_path / "a.md")

        call_count = [0]

        def scanner(d):
            call_count[0] += 1
            mtimes = {str(f): f.stat().st_mtime} if d and d.is_dir() else {}
            return (["v"], mtimes)

        cache = IndexCache(ttl_sec=60, scanner=scanner, glob_pattern="*.md")
        cache.get(tmp_path)
        assert call_count[0] == 1

        # Advance mtime by touching file in the future
        future = time.time() + 10
        import os
        os.utime(str(f), (future, future))

        cache.get(tmp_path)
        assert call_count[0] == 2, "should re-scan after mtime change"

    def test_no_refresh_when_mtime_unchanged(self, tmp_path):
        f = _write_file(tmp_path / "a.md")
        call_count = [0]

        def scanner(d):
            call_count[0] += 1
            mtimes = {str(f): f.stat().st_mtime} if d and d.is_dir() else {}
            return (["v"], mtimes)

        cache = IndexCache(ttl_sec=60, scanner=scanner, glob_pattern="*.md")
        cache.get(tmp_path)
        cache.get(tmp_path)
        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# File-set invalidation (additions and deletions) — the CFX-W5-2 fix
# ---------------------------------------------------------------------------

class TestFileSetInvalidation:
    def test_refreshes_when_new_file_added(self, tmp_path):
        """Adding a new file to the directory triggers re-scan."""
        f1 = _write_file(tmp_path / "a.md")
        call_count = [0]

        def scanner(d):
            call_count[0] += 1
            if d is None or not d.is_dir():
                return ([], {})
            files = list(d.glob("*.md"))
            return (["entries"], {str(f): f.stat().st_mtime for f in files})

        cache = IndexCache(ttl_sec=60, scanner=scanner, glob_pattern="*.md")
        cache.get(tmp_path)
        assert call_count[0] == 1

        _write_file(tmp_path / "b.md")
        cache.get(tmp_path)
        assert call_count[0] == 2, "should re-scan when new file added"

    def test_refreshes_when_file_deleted(self, tmp_path):
        """Deleting a file from the directory triggers re-scan."""
        f1 = _write_file(tmp_path / "a.md")
        f2 = _write_file(tmp_path / "b.md")
        call_count = [0]

        def scanner(d):
            call_count[0] += 1
            if d is None or not d.is_dir():
                return ([], {})
            files = list(d.glob("*.md"))
            return (["entries"], {str(f): f.stat().st_mtime for f in files})

        cache = IndexCache(ttl_sec=60, scanner=scanner, glob_pattern="*.md")
        cache.get(tmp_path)
        assert call_count[0] == 1

        f2.unlink()
        cache.get(tmp_path)
        assert call_count[0] == 2, "should re-scan when file deleted"


# ---------------------------------------------------------------------------
# Cache key isolation — the CFX-W5-2 dir_path fix
# ---------------------------------------------------------------------------

class TestCacheKeyIsolation:
    def test_different_dirs_cached_independently(self, tmp_path):
        """Two different dir_paths are cached with separate entries."""
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        _write_file(dir_a / "x.md")
        _write_file(dir_b / "y.md")

        results: dict = {}

        def scanner(d):
            if d is None or not d.is_dir():
                return ([], {})
            files = list(d.glob("*.md"))
            key = str(d)
            results[key] = [f.name for f in files]
            return ([f.name for f in files], {str(f): f.stat().st_mtime for f in files})

        cache = IndexCache(ttl_sec=60, scanner=scanner, glob_pattern="*.md")
        out_a = cache.get(dir_a)
        out_b = cache.get(dir_b)

        assert out_a != out_b, "different dirs should return different entries"
        assert "x.md" in out_a
        assert "y.md" in out_b

    def test_second_call_same_dir_does_not_rescan(self, tmp_path):
        """Same dir twice within TTL hits cache, not scanner."""
        _write_file(tmp_path / "a.md")
        call_count = [0]

        def scanner(d):
            call_count[0] += 1
            files = list(d.glob("*.md")) if d and d.is_dir() else []
            return (["v"], {str(f): f.stat().st_mtime for f in files})

        cache = IndexCache(ttl_sec=60, scanner=scanner, glob_pattern="*.md")
        cache.get(tmp_path)
        cache.get(tmp_path)
        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# None / missing dir handling
# ---------------------------------------------------------------------------

class TestNoneAndMissingDir:
    def test_none_dir_calls_scanner_uncached(self):
        calls = []

        def scanner(d):
            calls.append(d)
            return ([], {})

        cache = IndexCache(ttl_sec=60, scanner=scanner)
        cache.get(None)
        cache.get(None)
        assert len(calls) == 2, "None dir must NOT be cached"

    def test_missing_dir_calls_scanner_uncached(self, tmp_path):
        missing = tmp_path / "nonexistent"
        calls = []

        def scanner(d):
            calls.append(d)
            return ([], {})

        cache = IndexCache(ttl_sec=60, scanner=scanner)
        cache.get(missing)
        cache.get(missing)
        assert len(calls) == 2, "missing dir must NOT be cached"


# ---------------------------------------------------------------------------
# IndexCache.invalidate()
# ---------------------------------------------------------------------------

class TestInvalidate:
    def test_invalidate_specific_dir_forces_rescan(self, tmp_path):
        call_count = [0]

        def scanner(d):
            call_count[0] += 1
            return (["v"], {})

        cache = IndexCache(ttl_sec=60, scanner=scanner)
        cache.get(tmp_path)
        cache.invalidate(tmp_path)
        cache.get(tmp_path)
        assert call_count[0] == 2

    def test_invalidate_all_clears_all_entries(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        call_count = [0]

        def scanner(d):
            call_count[0] += 1
            return (["v"], {})

        cache = IndexCache(ttl_sec=60, scanner=scanner)
        cache.get(dir_a)
        cache.get(dir_b)
        assert call_count[0] == 2
        cache.invalidate(None)
        cache.get(dir_a)
        cache.get(dir_b)
        assert call_count[0] == 4
