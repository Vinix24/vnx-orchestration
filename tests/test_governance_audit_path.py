#!/usr/bin/env python3
"""Tests for governance_audit path canonicalization — fix/governance-audit-canonical-path."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

import governance_audit
from governance_enforcer import GovernanceEnforcer, AUDIT_LOG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state_audit(tmp_path: Path) -> Path:
    return tmp_path / "state" / "governance_audit.ndjson"


def _events_audit(tmp_path: Path) -> Path:
    return tmp_path / "events" / "governance_audit.ndjson"


def _read_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# ---------------------------------------------------------------------------
# Test 1: writer lands in state/, NOT events/
# ---------------------------------------------------------------------------

def test_write_lands_in_state_not_events(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    # Re-evaluate path so module picks up env var change
    import importlib
    importlib.reload(governance_audit)

    governance_audit.log_enforcement(
        check_name="test_check",
        level=2,
        result=True,
        context={"pr_number": 1},
        message="test entry",
    )

    assert _state_audit(tmp_path).exists(), "state/governance_audit.ndjson should exist"
    assert not _events_audit(tmp_path).exists(), "events/governance_audit.ndjson should NOT be written"

    entries = _read_entries(_state_audit(tmp_path))
    assert len(entries) == 1
    assert entries[0]["check_name"] == "test_check"


# ---------------------------------------------------------------------------
# Test 2: enforcer validator passes when state/ has entries
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = """\
version: 1
mode: standard

checks:
  decision_audit_trail:
    level: 2
    description: "governance_audit.ndjson must exist with at least one entry"

presets:
  standard: {}
"""


def test_enforcer_validator_passes_with_state_entries(tmp_path, monkeypatch, tmp_path_factory):
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    import importlib
    importlib.reload(governance_audit)

    # Write an entry (now goes to state/)
    governance_audit.log_enforcement(
        check_name="ci_green_required",
        level=3,
        result=True,
        context={"pr_number": 42},
        message="CI passed",
    )

    # Write minimal config
    config_path = tmp_path / "governance_enforcement.yaml"
    config_path.write_text(MINIMAL_CONFIG, encoding="utf-8")

    # Point enforcer at our tmp state path
    import governance_enforcer
    importlib.reload(governance_enforcer)
    monkeypatch.setattr(governance_enforcer, "AUDIT_LOG", tmp_path / "state" / "governance_audit.ndjson")

    enforcer = governance_enforcer.GovernanceEnforcer()
    enforcer.load_config(config_path)
    result = enforcer.check("decision_audit_trail", {})

    assert result.passed is True, f"Expected passed=True, got: {result.message}"
    assert "entry" in result.message.lower()


def test_enforcer_validator_fails_when_state_empty(tmp_path, monkeypatch):
    import importlib
    import governance_enforcer
    importlib.reload(governance_enforcer)

    # Ensure state/ audit does not exist
    audit_path = tmp_path / "state" / "governance_audit.ndjson"
    assert not audit_path.exists()

    config_path = tmp_path / "governance_enforcement.yaml"
    config_path.write_text(MINIMAL_CONFIG, encoding="utf-8")

    monkeypatch.setattr(governance_enforcer, "AUDIT_LOG", audit_path)

    enforcer = governance_enforcer.GovernanceEnforcer()
    enforcer.load_config(config_path)
    result = enforcer.check("decision_audit_trail", {})

    assert result.passed is False


# ---------------------------------------------------------------------------
# Test 3: migration script is idempotent
# ---------------------------------------------------------------------------

def _make_events_file(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "events" / "governance_audit.ndjson"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return p


def _make_state_file(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "state" / "governance_audit.ndjson"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return p


from migrate_governance_audit_path import migrate


def test_migration_basic(tmp_path):
    """Migration moves entries from events/ to state/ and removes source."""
    entries = [
        {"timestamp": "2026-01-01T00:00:00Z", "context_hash": "abc", "event_type": "gate_result", "check_name": "codex"},
        {"timestamp": "2026-01-01T00:01:00Z", "context_hash": "def", "event_type": "gate_result", "check_name": "gemini"},
    ]
    _make_events_file(tmp_path, entries)

    result = migrate(tmp_path)

    assert result["src_exists"] is True
    assert result["src_removed"] is True
    assert result["appended"] == 2
    assert result["skipped_dupes"] == 0
    assert not (tmp_path / "events" / "governance_audit.ndjson").exists()

    final = _read_entries(tmp_path / "state" / "governance_audit.ndjson")
    assert len(final) == 2


def test_migration_idempotent_no_dupes(tmp_path):
    """Running migration twice produces the same final state — no duplicate entries."""
    entries = [
        {"timestamp": "2026-01-01T00:00:00Z", "context_hash": "abc", "event_type": "gate_result", "check_name": "codex"},
    ]

    # First migration
    _make_events_file(tmp_path, entries)
    r1 = migrate(tmp_path)
    assert r1["appended"] == 1
    assert r1["src_removed"] is True

    # Re-create events/ file with same entries to simulate second run
    _make_events_file(tmp_path, entries)
    r2 = migrate(tmp_path)
    assert r2["appended"] == 0
    assert r2["skipped_dupes"] == 1
    assert r2["src_removed"] is True

    final = _read_entries(tmp_path / "state" / "governance_audit.ndjson")
    assert len(final) == 1, f"Expected 1 entry after idempotent run, got {len(final)}"


def test_migration_appends_new_entries_only(tmp_path):
    """When state/ already has some entries, only genuinely new ones are appended."""
    existing = [
        {"timestamp": "2026-01-01T00:00:00Z", "context_hash": "aaa", "event_type": "gate_result"},
    ]
    new_entries = [
        {"timestamp": "2026-01-01T00:00:00Z", "context_hash": "aaa", "event_type": "gate_result"},  # dupe
        {"timestamp": "2026-01-01T00:01:00Z", "context_hash": "bbb", "event_type": "enforcement_check"},  # new
    ]
    _make_state_file(tmp_path, existing)
    _make_events_file(tmp_path, new_entries)

    result = migrate(tmp_path)

    assert result["appended"] == 1
    assert result["skipped_dupes"] == 1

    final = _read_entries(tmp_path / "state" / "governance_audit.ndjson")
    assert len(final) == 2


def test_migration_dry_run_does_not_write(tmp_path):
    """Dry-run mode makes no filesystem changes."""
    entries = [{"timestamp": "2026-01-01T00:00:00Z", "context_hash": "xyz", "event_type": "gate_result"}]
    _make_events_file(tmp_path, entries)

    result = migrate(tmp_path, dry_run=True)

    assert result["dry_run"] is True
    assert (tmp_path / "events" / "governance_audit.ndjson").exists(), "Source must not be removed in dry-run"
    assert not (tmp_path / "state" / "governance_audit.ndjson").exists(), "Dest must not be created in dry-run"


def test_migration_no_source_is_noop(tmp_path):
    """Migration with no events/ file exits cleanly without creating state/ file."""
    result = migrate(tmp_path)

    assert result["src_exists"] is False
    assert result["src_removed"] is False
    assert not (tmp_path / "state" / "governance_audit.ndjson").exists()
