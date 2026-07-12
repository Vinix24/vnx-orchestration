#!/usr/bin/env python3
"""Tests for docs/core/SUBSYSTEMS.md — the cockpit status-ledger SSOT (P0-cockpit PR-1).

Asserts the seed ledger exists and carries the header the future `vnx subsystems --md`
generator (PR-3) must reproduce byte-for-byte.

Dispatch-ID: 20260712-173106-cockpit-pr1
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SUBSYSTEMS_MD = REPO / "docs" / "core" / "SUBSYSTEMS.md"


def test_subsystems_md_exists():
    assert SUBSYSTEMS_MD.is_file(), f"{SUBSYSTEMS_MD} does not exist"


def test_subsystems_md_has_header():
    content = SUBSYSTEMS_MD.read_text(encoding="utf-8")
    assert "| subsystem | what | flag | status | health |" in content


def test_subsystems_md_has_legend():
    content = SUBSYSTEMS_MD.read_text(encoding="utf-8")
    assert "**Legend:**" in content


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
