"""N5 (config-een-oppervlak): the merge-gate names are deleted.

A text scan over ``scripts/``, ``vnx_cli/``, ``dashboard/`` and ``bin/`` (Python and bash alike) finds
zero occurrences of each name. Code, comments and docstrings count: a name that is still written
down is a name someone can still believe in.

Where a name was a switch, the default path is what stays, and a behaviour test pins it: the
merge override reason comes only from the CLI argument.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import merge_preflight_ci_check as mpci

SCAN_DIRS = ("scripts", "vnx_cli", "dashboard", "bin")
SUFFIXES = {".py", ".sh", ""}

DELETED_NAMES = ("VNX_MERGE_OVERRIDE_REASON", "VNX_CI_WORKFLOW_NAME")


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
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # a binary file under bin/ holds no setting name
        for no, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{p.relative_to(VNX_ROOT).as_posix()}:{no}")
    return hits


@pytest.mark.parametrize("name", DELETED_NAMES)
def test_deleted_name_has_zero_occurrences(name):
    hits = _occurrences(name)
    assert not hits, f"{name}: {len(hits)} occurrence(s) left: {hits}"


def test_scan_sees_the_tree():
    """A scan over an empty tree would pass every name; prove it reads real files."""
    assert sum(1 for _ in _files()) > 100
    assert _occurrences("VNX_PROJECT_ID"), "the scan must find a name that is still read"


@pytest.mark.parametrize("value", ["operator says so", "", "   "])
def test_override_reason_is_not_read_from_the_environment(monkeypatch, value):
    monkeypatch.setenv("VNX_MERGE_OVERRIDE_REASON", value)
    assert mpci._resolve_override_reason(None) is None
    assert mpci._resolve_override_reason("  flag reason ") == "flag reason"

