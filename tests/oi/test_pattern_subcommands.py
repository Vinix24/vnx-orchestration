"""Tests for defer-pattern and wontfix-pattern subcommands."""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import open_items_manager as oim


def _make_item(item_id, title, severity="warn", status="open", created_days_ago=0):
    return {
        "id": item_id,
        "status": status,
        "severity": severity,
        "title": title,
        "details": "",
        "origin_dispatch_id": "test-dispatch",
        "origin_report_path": "",
        "pr_id": "",
        "created_at": (datetime.now() - timedelta(days=created_days_ago)).isoformat(),
        "updated_at": datetime.now().isoformat(),
        "closed_reason": None,
    }


def _write_items(oi_file, items):
    data = {"schema_version": "1.0", "items": items, "next_id": len(items) + 1}
    with open(oi_file, "w") as f:
        json.dump(data, f)
    return data


def _make_args(title_match, reason, severity=None, older_than_days=None, apply=False):
    return argparse.Namespace(
        title_match=title_match,
        reason=reason,
        severity=severity,
        older_than_days=older_than_days,
        apply=apply,
    )


@pytest.fixture
def oi_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    oi_file = state / "open_items.json"
    monkeypatch.setattr(oim, "STATE_DIR", state)
    monkeypatch.setattr(oim, "OPEN_ITEMS_FILE", oi_file)
    monkeypatch.setattr(oim, "DIGEST_FILE", state / "open_items_digest.json")
    monkeypatch.setattr(oim, "MARKDOWN_FILE", state / "open_items.md")
    monkeypatch.setattr(oim, "AUDIT_LOG", state / "open_items_audit.jsonl")
    yield state, oi_file


class TestDryRun:
    def test_dry_run_shows_count_no_mutation(self, oi_state):
        state, oi_file = oi_state
        items = [
            _make_item("OI-001", "Function exceeds warning threshold: 50 lines"),
            _make_item("OI-002", "Some other item"),
        ]
        _write_items(oi_file, items)

        args = _make_args("exceeds.*threshold", "hygiene wave", apply=False)
        oim._apply_pattern(args, "deferred")

        with open(oi_file) as f:
            data = json.load(f)
        assert all(i["status"] == "open" for i in data["items"]), "dry-run must not mutate"

        audit_file = state / "open_items_audit.jsonl"
        assert not audit_file.exists(), "dry-run must not write audit entries"


class TestApplyMutates:
    def test_apply_mutates_and_writes_audit(self, oi_state):
        state, oi_file = oi_state
        items = [
            _make_item("OI-001", "Function exceeds warning threshold: 50 lines"),
            _make_item("OI-002", "Function exceeds warning threshold: 30 lines"),
            _make_item("OI-003", "Unrelated item"),
        ]
        _write_items(oi_file, items)

        args = _make_args("exceeds.*threshold", "hygiene wave", apply=True)
        oim._apply_pattern(args, "deferred")

        with open(oi_file) as f:
            data = json.load(f)

        deferred = [i for i in data["items"] if i["status"] == "deferred"]
        open_items = [i for i in data["items"] if i["status"] == "open"]
        assert len(deferred) == 2, "two matching items must be deferred"
        assert len(open_items) == 1
        assert open_items[0]["id"] == "OI-003"
        assert all(i["closed_reason"] == "hygiene wave" for i in deferred)

        audit_file = state / "open_items_audit.jsonl"
        assert audit_file.exists(), "audit log must be written on apply"
        entries = [
            json.loads(line)
            for line in audit_file.read_text().splitlines()
            if line.strip()
        ]
        assert len(entries) == 2
        assert all(e["action"] == "pattern_close" for e in entries)
        assert all(e["to_status"] == "deferred" for e in entries)


class TestNoMatch:
    def test_regex_non_match_returns_empty(self, oi_state):
        state, oi_file = oi_state
        items = [
            _make_item("OI-001", "Module level import not at top"),
            _make_item("OI-002", "Some other item"),
        ]
        _write_items(oi_file, items)

        args = _make_args("tool_unavailable.*vulture", "no match reason", apply=True)
        oim._apply_pattern(args, "wontfix")

        with open(oi_file) as f:
            data = json.load(f)
        assert all(i["status"] == "open" for i in data["items"]), "no items mutated on no-match"

        audit_file = state / "open_items_audit.jsonl"
        empty = not audit_file.exists() or audit_file.read_text().strip() == ""
        assert empty, "no audit entries on no-match"


class TestSeverityFilter:
    def test_severity_filter_only_affects_matching_severity(self, oi_state):
        state, oi_file = oi_state
        items = [
            _make_item("OI-001", "File exceeds threshold: 100 lines", severity="warn"),
            _make_item("OI-002", "File exceeds threshold: 200 lines", severity="info"),
            _make_item("OI-003", "File exceeds threshold: 300 lines", severity="blocker"),
        ]
        _write_items(oi_file, items)

        args = _make_args("exceeds threshold", "hygiene", severity="warn", apply=True)
        oim._apply_pattern(args, "deferred")

        with open(oi_file) as f:
            data = json.load(f)

        deferred = [i for i in data["items"] if i["status"] == "deferred"]
        assert len(deferred) == 1
        assert deferred[0]["id"] == "OI-001"

        open_items = [i for i in data["items"] if i["status"] == "open"]
        assert len(open_items) == 2
        assert {i["id"] for i in open_items} == {"OI-002", "OI-003"}
