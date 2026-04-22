#!/usr/bin/env python3
"""Tests for OI-1115 — strict run_id validation in log_artifact."""

import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from log_artifact import _assert_safe_run_id


def test_run_id_path_traversal_rejected():
    with pytest.raises(ValueError, match="invalid run_id"):
        _assert_safe_run_id("../x")


def test_run_id_with_slashes_rejected():
    with pytest.raises(ValueError, match="invalid run_id"):
        _assert_safe_run_id("a/b")


def test_run_id_empty_rejected():
    with pytest.raises(ValueError, match="invalid run_id"):
        _assert_safe_run_id("")


def test_run_id_valid_accepted():
    assert _assert_safe_run_id("abc-123_v2") == "abc-123_v2"
