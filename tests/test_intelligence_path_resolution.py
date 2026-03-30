#!/usr/bin/env python3
"""Regression tests for intelligence import/export path helpers."""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import intelligence_export
import intelligence_import


def test_export_defaults_to_canonical_root_when_intelligence_dir_missing():
    paths = {
        "PROJECT_ROOT": "/tmp/worktree-runtime",
        "VNX_HOME": "/tmp/worktree-runtime",
        "VNX_CANONICAL_ROOT": "/tmp/main/.claude/vnx-system",
    }

    assert intelligence_export._intelligence_dir(paths) == Path(
        "/tmp/main/.claude/vnx-system/.vnx-intelligence"
    )


def test_import_defaults_to_canonical_root_when_intelligence_dir_missing():
    paths = {
        "PROJECT_ROOT": "/tmp/worktree-runtime",
        "VNX_HOME": "/tmp/worktree-runtime",
        "VNX_CANONICAL_ROOT": "/tmp/main/.claude/vnx-system",
    }

    assert intelligence_import._intelligence_dir(paths) == Path(
        "/tmp/main/.claude/vnx-system/.vnx-intelligence"
    )
