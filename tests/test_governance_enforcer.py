#!/usr/bin/env python3
"""Tests for governance_enforcer.py — F51-PR1."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Make scripts/lib importable
SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPT_DIR))

from governance_enforcer import (
    CheckConfig,
    EnforcementResult,
    GovernanceEnforcer,
    _level_label,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = """\
version: 1
mode: standard

checks:
  codex_gate_required:
    level: 2
    description: "Codex gate result must exist"
  kimi_gate_required:
    level: 2
    description: "Kimi gate result must exist"
  ci_green_required:
    level: 3
    description: "CI must be green"
  dead_code_check:
    level: 1
    description: "Dead code advisory"

presets:
  strict:
    codex_gate_required: 3
    kimi_gate_required: 3
    ci_green_required: 3
  relaxed:
    codex_gate_required: 0
    kimi_gate_required: 0
    ci_green_required: 1
  off:
    codex_gate_required: 0
    kimi_gate_required: 0
    ci_green_required: 0
    dead_code_check: 0
  standard: {}
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "governance_enforcement.yaml"
    p.write_text(MINIMAL_CONFIG)
    return p


@pytest.fixture
def gate_results_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state" / "review_gates" / "results"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def open_items_digest(tmp_path: Path) -> Path:
    p = tmp_path / "state" / "open_items_digest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"summary": {"blocker_count": 0}}))
    return p


@pytest.fixture
def audit_log(tmp_path: Path) -> Path:
    p = tmp_path / "state" / "governance_audit.ndjson"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Level label helper
# ---------------------------------------------------------------------------


def test_level_label_all_levels():
    assert _level_label(0) == "off"
    assert _level_label(1) == "advisory"
    assert _level_label(2) == "soft_mandatory"
    assert _level_label(3) == "hard_mandatory"
    assert _level_label(99) == "99"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_load_config_standard_mode(config_file: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    assert enforcer._mode == "standard"
    assert "codex_gate_required" in enforcer._checks
    assert enforcer._checks["codex_gate_required"].level == 2
    assert enforcer._checks["kimi_gate_required"].level == 2
    assert enforcer._checks["ci_green_required"].level == 3


def test_load_config_strict_preset(config_file: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file, mode_override="strict")
    assert enforcer._mode == "strict"
    assert enforcer._checks["codex_gate_required"].level == 3
    assert enforcer._checks["kimi_gate_required"].level == 3


def test_load_config_relaxed_preset(config_file: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file, mode_override="relaxed")
    assert enforcer._checks["codex_gate_required"].level == 0
    assert enforcer._checks["kimi_gate_required"].level == 0
    assert enforcer._checks["ci_green_required"].level == 1


def test_load_config_off_preset(config_file: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file, mode_override="off")
    for cfg in enforcer._checks.values():
        assert cfg.level == 0


# ---------------------------------------------------------------------------
# Check: disabled level=0
# ---------------------------------------------------------------------------


def test_check_disabled_level_skips(config_file: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file, mode_override="off")
    result = enforcer.check("codex_gate_required", {})
    assert result.passed is True
    assert result.level == 0


# ---------------------------------------------------------------------------
# Check: unknown check name
# ---------------------------------------------------------------------------


def test_check_unknown_name(config_file: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    result = enforcer.check("nonexistent_check", {})
    assert result.passed is True
    assert result.level == 0


# ---------------------------------------------------------------------------
# Check: codex_gate_required (OI-1884: a PASS only counts on the CURRENT head)
# ---------------------------------------------------------------------------

_HEAD_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_STALE_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_codex_gate_required_no_pr_number(config_file: Path, gate_results_dir: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {})
    assert result.passed is True
    assert "skipped" in result.message


def test_codex_gate_required_file_missing(config_file: Path, gate_results_dir: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 999, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "not found" in result.message


def test_codex_gate_required_pass_on_current_head_succeeds(config_file: Path, gate_results_dir: Path):
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({"status": "completed", "contract_hash": "abc123xyz", "commit_sha": _HEAD_SHA})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is True


def test_codex_gate_required_pass_on_stale_commit_is_refused(config_file: Path, gate_results_dir: Path):
    """OI-1884: a PASS recorded against an OLDER commit than the PR's current
    head must never satisfy the check — after a fix-forward push the code on
    the head was never reviewed."""
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({"contract_hash": "abc123xyz", "commit_sha": _STALE_SHA})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "oudere commit" in result.message
    assert _STALE_SHA[:8] in result.message
    assert _HEAD_SHA[:8] in result.message


def test_codex_gate_required_empty_hash(config_file: Path, gate_results_dir: Path):
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({"contract_hash": "", "commit_sha": _HEAD_SHA})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "empty contract_hash" in result.message


def test_codex_gate_required_missing_commit_sha_is_refused(config_file: Path, gate_results_dir: Path):
    """OI-1884: a record with no commit_sha at all (predates the field) is
    stale evidence, never a wildcard that matches any head."""
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({"contract_hash": "abc123xyz"})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "geen commit_sha" in result.message


def test_codex_gate_required_unresolvable_head_fails(config_file: Path, gate_results_dir: Path):
    """OI-1884: when the PR head cannot be determined at all, the check must
    fail closed rather than silently accept whatever PASS sits on disk."""
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({"contract_hash": "abc123xyz", "commit_sha": _HEAD_SHA})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "gh: no such pr"
        result = enforcer.check("codex_gate_required", {"pr_number": 42})
    assert result.passed is False
    assert "niet bepalen" in result.message


# ---------------------------------------------------------------------------
# Check: kimi_gate_required (OI-1884: a PASS only counts on the CURRENT head)
# ---------------------------------------------------------------------------


def test_kimi_gate_required_no_pr_number(config_file: Path, gate_results_dir: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("kimi_gate_required", {})
    assert result.passed is True
    assert "skipped" in result.message


def test_kimi_gate_required_file_missing(config_file: Path, gate_results_dir: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("kimi_gate_required", {"pr_number": 999, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "not found" in result.message


def test_kimi_gate_required_pass_on_current_head_succeeds(config_file: Path, gate_results_dir: Path):
    gate_results_dir.joinpath("pr-42-kimi_gate.json").write_text(
        json.dumps({"status": "completed", "contract_hash": "abc123xyz", "commit_sha": _HEAD_SHA})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("kimi_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is True


def test_kimi_gate_required_pass_on_stale_commit_is_refused(config_file: Path, gate_results_dir: Path):
    gate_results_dir.joinpath("pr-42-kimi_gate.json").write_text(
        json.dumps({"contract_hash": "abc123xyz", "commit_sha": _STALE_SHA})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("kimi_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "oudere commit" in result.message


def test_kimi_gate_required_empty_hash_no_takeover(config_file: Path, gate_results_dir: Path):
    gate_results_dir.joinpath("pr-42-kimi_gate.json").write_text(
        json.dumps({"contract_hash": "", "commit_sha": _HEAD_SHA})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("kimi_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "empty contract_hash" in result.message


def test_kimi_gate_required_missing_commit_sha_is_refused(config_file: Path, gate_results_dir: Path):
    gate_results_dir.joinpath("pr-42-kimi_gate.json").write_text(
        json.dumps({"contract_hash": "abc123xyz"})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("kimi_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "geen commit_sha" in result.message


def test_kimi_gate_required_unresolvable_head_fails(config_file: Path, gate_results_dir: Path):
    gate_results_dir.joinpath("pr-42-kimi_gate.json").write_text(
        json.dumps({"contract_hash": "abc123xyz", "commit_sha": _HEAD_SHA})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "gh: no such pr"
        result = enforcer.check("kimi_gate_required", {"pr_number": 42})
    assert result.passed is False
    assert "niet bepalen" in result.message


def test_kimi_gate_required_satisfied_by_takeover_successor_on_current_head(
    config_file: Path, gate_results_dir: Path
):
    """A kimi seat read by glm_gate through VNX_REVIEW_GATE_TAKEOVER_CHAIN
    satisfies the check the same way gate_enforcement_verify.py resolves a
    takeover seat: via the takeover_path hop recorded on the successor's own
    result, not a second takeover interpretation — but ONLY when the
    successor's own record carries the current head (OI-1884)."""
    gate_results_dir.joinpath("pr-42-glm_gate.json").write_text(
        json.dumps({
            "gate": "glm_gate",
            "status": "completed",
            "contract_hash": "deadbeef1234",
            "commit_sha": _HEAD_SHA,
            "takeover_path": [{"gate": "kimi_gate", "reason": "unavailable", "status": "unavailable"}],
        })
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("kimi_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is True
    assert "taken over" in result.message


def test_kimi_gate_required_takeover_successor_on_stale_commit_is_refused(
    config_file: Path, gate_results_dir: Path
):
    """OI-1884: a takeover successor's PASS on an OLDER commit than the
    current head is refused exactly like a direct PASS would be — it, too,
    never reviewed the code on the head."""
    gate_results_dir.joinpath("pr-42-glm_gate.json").write_text(
        json.dumps({
            "gate": "glm_gate",
            "contract_hash": "deadbeef1234",
            "commit_sha": _STALE_SHA,
            "takeover_path": [{"gate": "kimi_gate", "reason": "unavailable", "status": "unavailable"}],
        })
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("kimi_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "not found" in result.message


def test_kimi_gate_required_fails_without_result_or_takeover(config_file: Path, gate_results_dir: Path):
    """A takeover result for a DIFFERENT seat (codex_gate here) must never
    satisfy the kimi seat."""
    gate_results_dir.joinpath("pr-42-glm_gate.json").write_text(
        json.dumps({
            "gate": "glm_gate",
            "contract_hash": "deadbeef1234",
            "commit_sha": _HEAD_SHA,
            "takeover_path": [{"gate": "codex_gate", "reason": "unavailable", "status": "unavailable"}],
        })
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("kimi_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "not found" in result.message


# ---------------------------------------------------------------------------
# Regression: a record on the current head that is not a PASS must fail the
# seat (blocking findings / failed / unavailable / incomplete), direct or
# takeover. Before this fix only contract_hash + commit_sha were consulted, so
# a rejected or never-run gate read as a pass.
# ---------------------------------------------------------------------------

_BLOCKING_FINDING = {"severity": "blocker", "message": "unhandled error path"}


def test_codex_gate_required_blocking_finding_is_not_a_pass(config_file: Path, gate_results_dir: Path):
    """status completed + 1 blocking finding on the head is a decided non-pass."""
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({
            "status": "completed",
            "contract_hash": "abc123xyz",
            "commit_sha": _HEAD_SHA,
            "blocking_findings": [_BLOCKING_FINDING],
        })
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "not a PASS" in result.message
    assert "blocking finding" in result.message
    assert "codex_gate" in result.message
    assert "oudere commit" not in result.message
    assert "geen commit_sha" not in result.message
    assert "not found" not in result.message


def test_codex_gate_required_unavailable_is_not_a_pass(config_file: Path, gate_results_dir: Path):
    """An unavailable gate (provider outage) that echoes a contract_hash is not
    a pass: there is no verdict evidence."""
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({
            "status": "unavailable",
            "contract_hash": "abc123xyz",
            "commit_sha": _HEAD_SHA,
        })
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "not a PASS" in result.message
    assert "unavailable" in result.message
    assert "codex_gate" in result.message
    assert "oudere commit" not in result.message
    assert "geen commit_sha" not in result.message
    assert "not found" not in result.message


def test_codex_gate_required_failed_is_not_a_pass(config_file: Path, gate_results_dir: Path):
    """A failed review on the current head must fail the seat, naming status."""
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({
            "status": "failed",
            "contract_hash": "abc123xyz",
            "commit_sha": _HEAD_SHA,
        })
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "not a PASS" in result.message
    assert "failed" in result.message
    assert "codex_gate" in result.message
    assert "oudere commit" not in result.message
    assert "geen commit_sha" not in result.message
    assert "not found" not in result.message


def test_codex_gate_required_incomplete_is_not_a_pass(config_file: Path, gate_results_dir: Path):
    """An in-flight status (running) on the head is incomplete evidence, not a
    pass."""
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({
            "status": "running",
            "contract_hash": "abc123xyz",
            "commit_sha": _HEAD_SHA,
        })
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "not a PASS" in result.message
    assert "incomplete" in result.message
    assert "codex_gate" in result.message
    assert "oudere commit" not in result.message
    assert "geen commit_sha" not in result.message
    assert "not found" not in result.message


def test_kimi_gate_required_takeover_blocking_finding_is_not_a_pass(
    config_file: Path, gate_results_dir: Path
):
    """A takeover successor (glm_gate reads kimi_gate) that found a blocker is
    a decided non-pass: the successor does not satisfy the seat."""
    gate_results_dir.joinpath("pr-42-glm_gate.json").write_text(
        json.dumps({
            "gate": "glm_gate",
            "status": "completed",
            "contract_hash": "deadbeef1234",
            "commit_sha": _HEAD_SHA,
            "blocking_findings": [_BLOCKING_FINDING],
            "takeover_path": [{"gate": "kimi_gate", "reason": "unavailable", "status": "unavailable"}],
        })
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("kimi_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "not a PASS" in result.message
    assert "blocking finding" in result.message
    assert "glm_gate" in result.message
    assert "kimi_gate" in result.message
    assert "not found" not in result.message


def test_kimi_gate_required_takeover_unavailable_successor_is_not_a_pass(
    config_file: Path, gate_results_dir: Path
):
    """A takeover successor that is itself unavailable does not satisfy the seat."""
    gate_results_dir.joinpath("pr-42-glm_gate.json").write_text(
        json.dumps({
            "gate": "glm_gate",
            "status": "unavailable",
            "contract_hash": "deadbeef1234",
            "commit_sha": _HEAD_SHA,
            "takeover_path": [{"gate": "kimi_gate", "reason": "unavailable", "status": "unavailable"}],
        })
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("kimi_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is False
    assert "not a PASS" in result.message
    assert "unavailable" in result.message
    assert "glm_gate" in result.message


def test_genuine_pass_on_current_head_still_succeeds(config_file: Path, gate_results_dir: Path):
    """The shared pass rule needs a canonical pass status: 'approve' must still
    satisfy the check on the current head."""
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({
            "status": "approve",
            "contract_hash": "abc123xyz",
            "commit_sha": _HEAD_SHA,
        })
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 42, "head_sha": _HEAD_SHA})
    assert result.passed is True
    assert "passed on head" in result.message


# ---------------------------------------------------------------------------
# Check: no_blocking_open_items
# ---------------------------------------------------------------------------


def test_no_blocking_open_items_zero_blockers(config_file: Path, tmp_path: Path):
    digest = tmp_path / "oi.json"
    digest.write_text(json.dumps({"summary": {"blocker_count": 0}}))
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.OPEN_ITEMS_DIGEST", digest):
        result = enforcer._check_no_blocking_open_items(
            CheckConfig(name="no_blocking_open_items", level=3), {}
        )
    assert result.passed is True


def test_no_blocking_open_items_has_blockers(config_file: Path, tmp_path: Path):
    digest = tmp_path / "oi.json"
    digest.write_text(json.dumps({"summary": {"blocker_count": 3}}))
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.OPEN_ITEMS_DIGEST", digest):
        result = enforcer._check_no_blocking_open_items(
            CheckConfig(name="no_blocking_open_items", level=3), {}
        )
    assert result.passed is False
    assert "3" in result.message


def test_no_blocking_open_items_file_missing(config_file: Path, tmp_path: Path):
    missing = tmp_path / "nonexistent.json"
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.OPEN_ITEMS_DIGEST", missing):
        result = enforcer._check_no_blocking_open_items(
            CheckConfig(name="no_blocking_open_items", level=3), {}
        )
    assert result.passed is True
    assert "skipped" in result.message


# ---------------------------------------------------------------------------
# Check: decision_audit_trail
# ---------------------------------------------------------------------------


def test_decision_audit_trail_file_missing(config_file: Path, tmp_path: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    # Point VNX_DATA_DIR at empty tmp_path so get_recent() finds neither state/ nor events/
    with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
        result = enforcer._check_decision_audit_trail(
            CheckConfig(name="decision_audit_trail", level=3), {}
        )
    assert result.passed is False


def test_decision_audit_trail_has_entries(config_file: Path, tmp_path: Path):
    audit = tmp_path / "state" / "governance_audit.ndjson"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text('{"timestamp": "2026-04-13T00:00:00Z", "event_type": "gate_result"}\n')
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
        result = enforcer._check_decision_audit_trail(
            CheckConfig(name="decision_audit_trail", level=3), {}
        )
    assert result.passed is True


# ---------------------------------------------------------------------------
# Override mechanism
# ---------------------------------------------------------------------------


def test_soft_mandatory_override_accepted(config_file: Path, gate_results_dir: Path, tmp_path: Path):
    audit = tmp_path / "governance_audit.ndjson"
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)  # codex_gate_required is level 2

    # File missing → would fail
    env = {"VNX_OVERRIDE_CODEX_GATE_REQUIRED": "manual-verification-done"}
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir), \
         patch("governance_enforcer.AUDIT_LOG", audit), \
         patch.dict(os.environ, env, clear=False):
        result = enforcer.check("codex_gate_required", {"pr_number": 999, "head_sha": _HEAD_SHA})

    assert result.passed is True
    assert result.overridden_by == "manual-verification-done"
    assert "[OVERRIDDEN]" in result.message
    # Override must be logged
    assert audit.exists()
    log_entries = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
    assert any(e["event"] == "override_accepted" for e in log_entries)


def test_hard_mandatory_override_rejected(config_file: Path, gate_results_dir: Path, tmp_path: Path):
    audit = tmp_path / "governance_audit.ndjson"
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)  # ci_green_required is level 3

    # Simulate failed check — no gh CLI available but mocked
    env = {"VNX_OVERRIDE_CI_GREEN_REQUIRED": "i-promise-its-green"}
    with patch("governance_enforcer.AUDIT_LOG", audit), \
         patch.dict(os.environ, env, clear=False), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = "[]"
        mock_run.return_value.stderr = "error"
        result = enforcer.check("ci_green_required", {"pr_number": 100})

    # Hard mandatory cannot be overridden
    assert result.passed is False
    assert result.overridden_by is None


# ---------------------------------------------------------------------------
# is_blocked / has_soft_failures
# ---------------------------------------------------------------------------


def test_is_blocked_returns_true_on_hard_failure(config_file: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    results = [
        EnforcementResult("codex_gate_required", 2, False, "fail", "VNX_OVERRIDE_CODEX_GATE_REQUIRED"),
        EnforcementResult("ci_green_required", 3, False, "fail", "VNX_OVERRIDE_CI_GREEN_REQUIRED"),
    ]
    assert enforcer.is_blocked(results) is True


def test_is_blocked_false_when_only_soft_fails(config_file: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    results = [
        EnforcementResult("codex_gate_required", 2, False, "fail", "VNX_OVERRIDE_CODEX_GATE_REQUIRED"),
    ]
    assert enforcer.is_blocked(results) is False


def test_is_blocked_false_when_all_pass(config_file: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    results = [
        EnforcementResult("codex_gate_required", 2, True, "ok", "VNX_OVERRIDE_CODEX_GATE_REQUIRED"),
        EnforcementResult("ci_green_required", 3, True, "ok", "VNX_OVERRIDE_CI_GREEN_REQUIRED"),
    ]
    assert enforcer.is_blocked(results) is False


# ---------------------------------------------------------------------------
# CLI: list command
# ---------------------------------------------------------------------------


def test_cli_list_returns_zero(config_file: Path):
    rc = main(["list", "--config", str(config_file)])
    assert rc == 0


def test_cli_check_no_context(config_file: Path, tmp_path: Path):
    audit = tmp_path / "governance_audit.ndjson"
    with patch("governance_enforcer.AUDIT_LOG", audit), \
         patch("governance_enforcer.GATE_RESULTS_DIR", tmp_path / "gate_results"), \
         patch("governance_enforcer.OPEN_ITEMS_DIGEST", tmp_path / "oi.json"):
        # ci_green_required level=3 will skip (no pr_number) but some checks may fail
        rc = main(["check", "--config", str(config_file)])
    # May return 0 or 1 depending on what checks pass without context, but must not crash
    assert rc in (0, 1)


def test_cli_check_invalid_json_context(config_file: Path):
    rc = main(["check", "--config", str(config_file), "--context", "{invalid json}"])
    assert rc == 2


def test_cli_no_command_returns_zero(config_file: Path):
    rc = main([])
    assert rc == 0


# ---------------------------------------------------------------------------
# build_codex_prompt fix — vertex_ai_runner
# ---------------------------------------------------------------------------


def test_build_codex_prompt_inlines_file_contents(tmp_path: Path):
    """build_codex_prompt should inline file contents, not mention PR number."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
    from vertex_ai_runner import build_codex_prompt

    test_file = tmp_path / "test_script.py"
    test_file.write_text("def foo(): pass\n")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class R:
            returncode = 0
            stdout = str(test_file)
            stderr = ""
        return R()

    result = build_codex_prompt(
        {
            "changed_files": [str(test_file)],
            "branch": "feat/f51",
            "risk_class": "medium",
            "pr_number": 221,
        },
        subprocess_run=fake_run,
    )

    assert "PR #221" not in result, "Prompt must not reference PR number for GitHub API"
    assert "feat/f51" in result
    assert "def foo(): pass" in result
    assert "```json" in result


def test_build_codex_prompt_fallback_to_git_diff(tmp_path: Path):
    """When changed_files is empty, fall back to git diff output."""
    from vertex_ai_runner import build_codex_prompt

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = ""  # no files returned
            stderr = ""
        return R()

    result = build_codex_prompt(
        {"changed_files": [], "branch": "feat/f51", "risk_class": "low"},
        subprocess_run=fake_run,
    )
    # With no files and empty git diff, file_contents will be empty string
    assert "Review the following code changes" in result
    assert "```json" in result
