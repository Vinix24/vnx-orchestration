"""query_inspector — flags potentially unsafe SQL-building functions.

CORRECT as-shipped. The test that uses this is broken (mock-pollution).
Do not modify this file.
"""
from __future__ import annotations

import inspect
import re


_UNSAFE_PATTERNS = [
    re.compile(r"f['\"]\s*SELECT", re.IGNORECASE),
    re.compile(r"f['\"]\s*INSERT", re.IGNORECASE),
    re.compile(r"f['\"]\s*UPDATE", re.IGNORECASE),
    re.compile(r"f['\"]\s*DELETE", re.IGNORECASE),
    re.compile(r"%\s*\(.*?\)\s*s\b", re.IGNORECASE),
    re.compile(r"\.format\s*\(", re.IGNORECASE),
]


def detect_unsafe(fn) -> bool:
    """Return True if fn's source contains an unsafe SQL-building pattern."""
    source = inspect.getsource(fn)
    return any(p.search(source) for p in _UNSAFE_PATTERNS)
