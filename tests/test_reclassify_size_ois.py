#!/usr/bin/env python3
"""Tests for reclassify_size_ois backfill script."""

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "maintenance"))

from reclassify_size_ois import _is_size_oi, reclassify


def _make_store(tmp_path, items):
    store = tmp_path / "open_items.json"
    data = {"schema_version": "1.0", "items": items, "next_id": len(items) + 1}
    store.write_text(json.dumps(data, indent=2))
    return store


def _audit(tmp_path):
    return tmp_path / "open_items_audit.jsonl"


# ---------------------------------------------------------------------------
# _is_size_oi matching
# ---------------------------------------------------------------------------

class TestIsSizeOi:
    def test_dedup_key_function_size(self):
        assert _is_size_oi({"dedup_key": "qa:function_size_blocking:foo"})

    def test_dedup_key_file_size(self):
        assert _is_size_oi({"dedup_key": "qa:file_size_blocking:bar"})

    def test_title_exceeds_blocking_threshold(self):
        assert _is_size_oi({"title": "Function exceeds blocking threshold: 80 lines"})

    def test_title_file_exceeds_blocking_threshold(self):
        assert _is_size_oi({"title": "File exceeds blocking threshold: 900 lines (max 800)"})

    def test_title_exceeds_threshold_function(self):
        assert _is_size_oi({"title": "function foo exceeds threshold: 50 lines"})

    def test_unrelated_oi_not_matched(self):
        assert not _is_size_oi({"title": "Missing test coverage for src/foo.py"})

    def test_lint_oi_not_matched(self):
        assert not _is_size_oi({"dedup_key": "qa:lint_e501:bar", "title": "Long line"})

    def test_empty_item_not_matched(self):
        assert not _is_size_oi({})


# ---------------------------------------------------------------------------
# reclassify — dry-run mode
# ---------------------------------------------------------------------------

class TestReclassifyDryRun:
    def test_dry_run_does_not_modify_store(self, tmp_path):
        items = [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "title": "File exceeds blocking threshold: 900 lines", "updated_at": "2026-01-01"},
        ]
        store = _make_store(tmp_path, items)
        original = store.read_text()

        result = reclassify(store, _audit(tmp_path), apply=False)

        assert result["found"] == 1
        assert result["reclassified"] == 0
        assert store.read_text() == original

    def test_dry_run_no_audit_entries(self, tmp_path):
        items = [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "title": "File exceeds blocking threshold: 900 lines", "updated_at": "2026-01-01"},
        ]
        store = _make_store(tmp_path, items)
        audit = _audit(tmp_path)

        reclassify(store, audit, apply=False)

        assert not audit.exists()

    def test_dry_run_returns_correct_found_count(self, tmp_path):
        items = [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "title": "File exceeds blocking threshold: 900 lines", "updated_at": "2026-01-01"},
            {"id": "OI-002", "status": "open", "severity": "blocker",
             "dedup_key": "qa:function_size_blocking:foo", "title": "x", "updated_at": "2026-01-01"},
            {"id": "OI-003", "status": "open", "severity": "warn",
             "title": "Missing test coverage", "updated_at": "2026-01-01"},
        ]
        store = _make_store(tmp_path, items)

        result = reclassify(store, _audit(tmp_path), apply=False)

        assert result["found"] == 2


# ---------------------------------------------------------------------------
# reclassify — apply mode
# ---------------------------------------------------------------------------

class TestReclassifyApply:
    def test_apply_reclassifies_size_blockers(self, tmp_path):
        items = [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "title": "File exceeds blocking threshold: 900 lines", "updated_at": "2026-01-01"},
        ]
        store = _make_store(tmp_path, items)

        result = reclassify(store, _audit(tmp_path), apply=True)

        assert result["found"] == 1
        assert result["reclassified"] == 1
        data = json.loads(store.read_text())
        assert data["items"][0]["severity"] == "warn"

    def test_apply_does_not_touch_non_size_blockers(self, tmp_path):
        items = [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "title": "File exceeds blocking threshold: 900 lines", "updated_at": "2026-01-01"},
            {"id": "OI-002", "status": "open", "severity": "blocker",
             "title": "Missing test coverage", "updated_at": "2026-01-01"},
        ]
        store = _make_store(tmp_path, items)

        result = reclassify(store, _audit(tmp_path), apply=True)

        assert result["found"] == 1
        data = json.loads(store.read_text())
        oi2 = next(i for i in data["items"] if i["id"] == "OI-002")
        assert oi2["severity"] == "blocker"

    def test_apply_does_not_touch_closed_items(self, tmp_path):
        items = [
            {"id": "OI-001", "status": "done", "severity": "blocker",
             "title": "File exceeds blocking threshold: 900 lines", "updated_at": "2026-01-01"},
        ]
        store = _make_store(tmp_path, items)

        result = reclassify(store, _audit(tmp_path), apply=True)

        assert result["found"] == 0
        data = json.loads(store.read_text())
        assert data["items"][0]["severity"] == "blocker"

    def test_apply_writes_audit_entries(self, tmp_path):
        items = [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "dedup_key": "qa:file_size_blocking:scripts/lib/foo.py",
             "title": "x", "updated_at": "2026-01-01"},
        ]
        store = _make_store(tmp_path, items)
        audit = _audit(tmp_path)

        reclassify(store, audit, apply=True)

        assert audit.exists()
        entries = [json.loads(line) for line in audit.read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["action"] == "reclassify"
        assert entries[0]["item_id"] == "OI-001"
        assert entries[0]["from_severity"] == "blocker"
        assert entries[0]["to_severity"] == "warn"

    def test_apply_idempotent(self, tmp_path):
        items = [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "title": "File exceeds blocking threshold: 900 lines", "updated_at": "2026-01-01"},
        ]
        store = _make_store(tmp_path, items)
        audit = _audit(tmp_path)

        reclassify(store, audit, apply=True)
        result2 = reclassify(store, audit, apply=True)

        assert result2["found"] == 0
        assert result2["reclassified"] == 0

    def test_apply_matches_dedup_key_prefix(self, tmp_path):
        items = [
            {"id": "OI-001", "status": "open", "severity": "blocker",
             "dedup_key": "qa:function_size_blocking:scripts/lib/bar.py:my_func",
             "title": "x", "updated_at": "2026-01-01"},
        ]
        store = _make_store(tmp_path, items)

        result = reclassify(store, _audit(tmp_path), apply=True)

        assert result["reclassified"] == 1

    def test_apply_empty_store_no_error(self, tmp_path):
        store = _make_store(tmp_path, [])

        result = reclassify(store, _audit(tmp_path), apply=True)

        assert result["found"] == 0
        assert result["reclassified"] == 0
