#!/usr/bin/env python3
"""Tests for OI auto-close rescan functionality."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from argparse import Namespace

import pytest

# Ensure scripts/ is on path for imports
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import open_items_manager as oim


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Set up temp state directory and VNX_ROOT for OI testing."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(oim, "STATE_DIR", state_dir)
    monkeypatch.setattr(oim, "OPEN_ITEMS_FILE", state_dir / "open_items.json")
    monkeypatch.setattr(oim, "DIGEST_FILE", state_dir / "open_items_digest.json")
    monkeypatch.setattr(oim, "MARKDOWN_FILE", state_dir / "open_items.md")
    monkeypatch.setattr(oim, "AUDIT_LOG", state_dir / "open_items_audit.jsonl")
    monkeypatch.setattr(oim, "VNX_ROOT", tmp_path)
    return tmp_path


def _make_items(items):
    """Build an open_items data structure."""
    return {"schema_version": "1.0", "items": items, "next_id": len(items) + 1}


def _save(data, state_dir):
    """Save items to the patched state dir."""
    with open(state_dir / "state" / "open_items.json", "w") as f:
        json.dump(data, f)


class TestCheckViolationFileSize:
    def test_file_deleted(self, tmp_state):
        item = {"title": "file scripts/lib/foo.py exceeds 300L", "status": "open"}
        result = oim.check_violation(item)
        assert result is not None
        assert result["resolved"] is True
        assert "no longer exists" in result["reason"]

    def test_file_under_threshold(self, tmp_state):
        # Create a file with 50 lines
        target = tmp_state / "scripts" / "lib" / "small.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(f"line {i}" for i in range(50)))

        item = {"title": "file scripts/lib/small.py exceeds 300L", "status": "open"}
        result = oim.check_violation(item)
        assert result["resolved"] is True
        assert "actual 50L" in result["reason"]
        assert "threshold 300L" in result["reason"]

    def test_file_still_exceeds(self, tmp_state):
        target = tmp_state / "scripts" / "lib" / "big.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(f"line {i}" for i in range(400)))

        item = {"title": "file scripts/lib/big.py exceeds 300L", "status": "open"}
        result = oim.check_violation(item)
        assert result["resolved"] is False
        assert "still exceeds" in result["reason"]

    def test_file_size_pattern_with_lines_word(self, tmp_state):
        item = {"title": "file scripts/foo.sh exceeds 200 lines", "status": "open"}
        result = oim.check_violation(item)
        assert result is not None
        assert result["resolved"] is True  # file doesn't exist


class TestCheckViolationFunctionSize:
    def test_function_removed(self, tmp_state):
        target = tmp_state / "scripts" / "lib" / "mod.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("class Foo:\n    pass\n")

        item = {"title": "function big_func in scripts/lib/mod.py exceeds 50L", "status": "open"}
        result = oim.check_violation(item)
        assert result["resolved"] is True
        assert "no longer exists" in result["reason"]

    def test_function_shrunk(self, tmp_state):
        target = tmp_state / "scripts" / "lib" / "mod.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = ["def small_func():", "    x = 1", "    return x", "", "def other():"]
        target.write_text("\n".join(lines))

        item = {"title": "function small_func in scripts/lib/mod.py exceeds 50L", "status": "open"}
        result = oim.check_violation(item)
        assert result["resolved"] is True
        assert "actual 4L" in result["reason"]

    def test_function_still_exceeds(self, tmp_state):
        target = tmp_state / "scripts" / "lib" / "mod.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        body_lines = [f"    line_{i} = {i}" for i in range(60)]
        lines = ["def big_func():"] + body_lines + ["", "def other():"]
        target.write_text("\n".join(lines))

        item = {"title": "function big_func in scripts/lib/mod.py exceeds 50L", "status": "open"}
        result = oim.check_violation(item)
        assert result["resolved"] is False
        assert "still exceeds" in result["reason"]

    def test_no_file_path_in_title(self, tmp_state):
        item = {"title": "function orphan exceeds 30L", "status": "open"}
        result = oim.check_violation(item)
        assert result is not None
        assert result["resolved"] is False
        assert "no file path" in result["reason"]

    def test_file_deleted_for_function(self, tmp_state):
        item = {"title": "function gone_func in scripts/lib/deleted.py exceeds 50L", "status": "open"}
        result = oim.check_violation(item)
        assert result["resolved"] is True
        assert "file no longer exists" in result["reason"]


class TestCheckViolationNonMatching:
    def test_non_matching_title_returns_none(self, tmp_state):
        item = {"title": "Missing error handling in dispatch_broker.py", "status": "open"}
        result = oim.check_violation(item)
        assert result is None


class TestRescanItems:
    def test_rescan_closes_resolved(self, tmp_state):
        # File that doesn't exist -> auto-close
        data = _make_items([
            {
                "id": "OI-001",
                "status": "open",
                "severity": "warn",
                "title": "file scripts/lib/gone.py exceeds 300L",
                "details": "",
                "origin_dispatch_id": "d-old",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
                "closed_reason": None,
            },
        ])
        _save(data, tmp_state)

        args = Namespace(dry_run=False)
        oim.rescan_items(args)

        reloaded = oim.load_items()
        assert reloaded["items"][0]["status"] == "done"
        assert "no longer exists" in reloaded["items"][0]["closed_reason"]
        assert reloaded["items"][0]["closed_by"] == "auto-rescan"

    def test_rescan_dry_run(self, tmp_state):
        data = _make_items([
            {
                "id": "OI-002",
                "status": "open",
                "severity": "info",
                "title": "file scripts/lib/gone2.py exceeds 200L",
                "details": "",
                "origin_dispatch_id": "d-old",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
                "closed_reason": None,
            },
        ])
        _save(data, tmp_state)

        args = Namespace(dry_run=True)
        oim.rescan_items(args)

        reloaded = oim.load_items()
        assert reloaded["items"][0]["status"] == "open"  # NOT closed in dry run

    def test_rescan_skips_already_closed(self, tmp_state):
        data = _make_items([
            {
                "id": "OI-003",
                "status": "done",
                "severity": "warn",
                "title": "file scripts/lib/old.py exceeds 300L",
                "details": "",
                "origin_dispatch_id": "d-old",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
                "closed_reason": "manually closed",
            },
        ])
        _save(data, tmp_state)

        args = Namespace(dry_run=False)
        oim.rescan_items(args)

        reloaded = oim.load_items()
        assert reloaded["items"][0]["status"] == "done"
        assert reloaded["items"][0]["closed_reason"] == "manually closed"

    def test_rescan_leaves_unresolved(self, tmp_state):
        # Create a file that still exceeds
        target = tmp_state / "scripts" / "lib" / "still_big.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(f"line {i}" for i in range(400)))

        data = _make_items([
            {
                "id": "OI-004",
                "status": "open",
                "severity": "warn",
                "title": "file scripts/lib/still_big.py exceeds 300L",
                "details": "",
                "origin_dispatch_id": "d-old",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
                "closed_reason": None,
            },
        ])
        _save(data, tmp_state)

        args = Namespace(dry_run=False)
        oim.rescan_items(args)

        reloaded = oim.load_items()
        assert reloaded["items"][0]["status"] == "open"

    def test_rescan_writes_audit_log(self, tmp_state):
        data = _make_items([
            {
                "id": "OI-005",
                "status": "open",
                "severity": "warn",
                "title": "file scripts/lib/deleted.py exceeds 300L",
                "details": "",
                "origin_dispatch_id": "d-old",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
                "closed_reason": None,
            },
        ])
        _save(data, tmp_state)

        args = Namespace(dry_run=False)
        oim.rescan_items(args)

        audit_path = tmp_state / "state" / "open_items_audit.jsonl"
        assert audit_path.exists()
        entries = [json.loads(l) for l in audit_path.read_text().strip().split("\n") if l]
        auto_close_entries = [e for e in entries if e["action"] == "auto_close"]
        assert len(auto_close_entries) == 1
        assert auto_close_entries[0]["item_id"] == "OI-005"
        assert auto_close_entries[0]["source"] == "rescan"
