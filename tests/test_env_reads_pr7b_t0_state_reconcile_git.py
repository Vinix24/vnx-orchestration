"""PR-7b part b3 (config-een-oppervlak): the t0-state and reconcile-git names are deleted.

A text scan over ``scripts/``, ``vnx_cli/``, ``dashboard/`` and ``bin/`` finds zero occurrences of each name.
One file per PR-7b part, so the five parts merge in any order without a conflict.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("scripts", "vnx_cli", "dashboard", "bin")
SUFFIXES = {".py", ".sh", ""}

DELETED_NAMES = (
    "VNX_BESLUITEN_LOGDIR", "VNX_STALE_STAGED_MINUTES", "VNX_T0_DETAIL_RETENTION_DAYS",
    "VNX_RECONCILE_GIT",
)
EXEMPT: dict = {}


def _files():
    for d in SCAN_DIRS:
        base = VNX_ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix in SUFFIXES and "__pycache__" not in p.parts:
                yield p


def _occurrences(name: str):
    pattern = re.compile(r"(?<![A-Z0-9_])" + re.escape(name) + r"(?![A-Z0-9_])")
    hits = []
    for p in _files():
        rel = p.relative_to(VNX_ROOT).as_posix()
        if rel in EXEMPT.get(name, ()):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # a binary file under bin/ holds no setting name
        for no, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{rel}:{no}")
    return hits


@pytest.mark.parametrize("name", DELETED_NAMES)
def test_deleted_name_has_zero_occurrences(name):
    hits = _occurrences(name)
    assert not hits, f"{name}: {len(hits)} occurrence(s) left: {hits}"


def test_scan_sees_the_tree():
    """A scan over an empty tree would pass every name; prove it reads real files."""
    assert sum(1 for _ in _files()) > 100
    assert _occurrences("VNX_PROJECT_ID"), "the scan must find a name that is still read"
