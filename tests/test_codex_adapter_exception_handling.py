#!/usr/bin/env python3
"""Exception-handling regression tests for codex_adapter.py (OI-1437).

Covers two narrowed sites:
- line 602: OSError from _write_token_cache file write
- line 621: (OSError, json.JSONDecodeError) from get_token_usage file read/parse
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR / "lib" / "adapters"))


def test_runs_clean_on_default_env():
    """codex_adapter module imports without raising."""
    import codex_adapter  # noqa: F401


def test_write_token_cache_oserror_swallowed(caplog, tmp_path):
    """OSError during token cache write is caught and logged at DEBUG."""
    from codex_adapter import CodexAdapter

    adapter = object.__new__(CodexAdapter)
    adapter._terminal_id = "T1"

    with patch.object(Path, "write_text", side_effect=OSError("disk full")), \
         patch.object(Path, "mkdir"), \
         caplog.at_level(logging.DEBUG, logger="codex_adapter"):
        adapter._write_token_cache(
            {"input_tokens": 100, "output_tokens": 50},
            state_dir=tmp_path,
        )

    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "disk full" in debug_msgs


def test_get_token_usage_corrupt_json_swallowed(caplog, tmp_path):
    """json.JSONDecodeError from corrupt cache file is caught and logged at DEBUG."""
    from codex_adapter import CodexAdapter

    cache_dir = tmp_path / "token_cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "T1_usage.json"
    cache_file.write_text("{ invalid json }", encoding="utf-8")

    with patch.dict(os.environ, {"VNX_STATE_DIR": str(tmp_path)}), \
         caplog.at_level(logging.DEBUG, logger="codex_adapter"):
        result = CodexAdapter.get_token_usage("T1")

    assert result is None
    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "T1" in debug_msgs


def test_get_token_usage_oserror_swallowed(caplog, tmp_path):
    """OSError from reading cache file is caught and logged at DEBUG."""
    from codex_adapter import CodexAdapter

    cache_dir = tmp_path / "token_cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "T1_usage.json"
    cache_file.write_text("{}", encoding="utf-8")

    with patch.dict(os.environ, {"VNX_STATE_DIR": str(tmp_path)}), \
         patch.object(Path, "read_text", side_effect=OSError("read error")), \
         caplog.at_level(logging.DEBUG, logger="codex_adapter"):
        result = CodexAdapter.get_token_usage("T1")

    assert result is None
    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "read error" in debug_msgs
