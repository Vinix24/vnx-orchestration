"""PR-7b part b4 (config-een-oppervlak): the learning, dream, suppress-window, footer and pool names are deleted.

A text scan over ``scripts/``, ``vnx_cli/``, ``dashboard/`` and ``bin/`` (Python and bash alike) finds
zero occurrences of each name. Code, comments and docstrings count: a name that is still written
down is a name someone can still believe in. A separate file per part, so the five PR-7b PRs merge
in any order.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("scripts", "vnx_cli", "dashboard", "bin")
SUFFIXES = {".py", ".sh", ""}

DELETED_NAMES = (
    "VNX_LEARN_PERSIST", "VNX_LEARN_SUPERSEDE", "VNX_DREAM_TIMEOUT", "VNX_INTEL_SUPPRESS_WINDOW",
    "VNX_WORKER_RULES_FOOTER", "VNX_DISPATCH_FOOTER_MODE", "VNX_POOL_TASK_CONSUMER",
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
