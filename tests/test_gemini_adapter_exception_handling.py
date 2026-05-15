#!/usr/bin/env python3
"""Exception-handling regression tests for gemini_adapter.py (OI-1437).

Covers two narrowed sites:
- line 580: OSError from _write_token_cache file write
- line 599: (OSError, json.JSONDecodeError) from get_token_usage file read/parse
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
    """gemini_adapter module imports without raising."""
    import gemini_adapter  # noqa: F401


def test_write_token_cache_oserror_swallowed(caplog, tmp_path):
    """OSError during token cache write is caught and logged at DEBUG."""
    from gemini_adapter import GeminiAdapter

    adapter = object.__new__(GeminiAdapter)
    adapter._terminal_id = "T3"

    with patch.object(Path, "write_text", side_effect=OSError("disk full")), \
         patch.object(Path, "mkdir"), \
         caplog.at_level(logging.DEBUG, logger="gemini_adapter"):
        adapter._write_token_cache(
            {"input_tokens": 200, "output_tokens": 80},
            state_dir=tmp_path,
        )

    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "disk full" in debug_msgs


def test_get_token_usage_corrupt_json_swallowed(caplog, tmp_path):
    """json.JSONDecodeError from corrupt cache file is caught and logged at DEBUG."""
    from gemini_adapter import GeminiAdapter

    cache_dir = tmp_path / "token_cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "T3_usage.json"
    cache_file.write_text("not json at all", encoding="utf-8")

    with patch.dict(os.environ, {"VNX_STATE_DIR": str(tmp_path)}), \
         caplog.at_level(logging.DEBUG, logger="gemini_adapter"):
        result = GeminiAdapter.get_token_usage("T3")

    assert result is None
    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "T3" in debug_msgs


def test_get_token_usage_oserror_swallowed(caplog, tmp_path):
    """OSError from reading cache file is caught and logged at DEBUG."""
    from gemini_adapter import GeminiAdapter

    cache_dir = tmp_path / "token_cache"
    cache_dir.mkdir()
    (cache_dir / "T3_usage.json").write_text("{}", encoding="utf-8")

    with patch.dict(os.environ, {"VNX_STATE_DIR": str(tmp_path)}), \
         patch.object(Path, "read_text", side_effect=OSError("permission denied")), \
         caplog.at_level(logging.DEBUG, logger="gemini_adapter"):
        result = GeminiAdapter.get_token_usage("T3")

    assert result is None
    debug_msgs = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
    assert "permission denied" in debug_msgs
