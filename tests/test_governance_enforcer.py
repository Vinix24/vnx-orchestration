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
  ci_green_required:
    level: 3
    description: "CI must be green"
  dead_code_check:
    level: 1
    description: "Dead code advisory"

presets:
  strict:
    codex_gate_required: 3
    ci_green_required: 3
  relaxed:
    codex_gate_required: 0
    ci_green_required: 1
  off:
    codex_gate_required: 0
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
    assert enforcer._checks["ci_green_required"].level == 3


def test_load_config_strict_preset(config_file: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file, mode_override="strict")
    assert enforcer._mode == "strict"
    assert enforcer._checks["codex_gate_required"].level == 3


def test_load_config_relaxed_preset(config_file: Path):
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file, mode_override="relaxed")
    assert enforcer._checks["codex_gate_required"].level == 0
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
# Check: codex_gate_required
# ---------------------------------------------------------------------------


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
        result = enforcer.check("codex_gate_required", {"pr_number": 999})
    assert result.passed is False
    assert "not found" in result.message


def test_codex_gate_required_file_present_with_hash(config_file: Path, gate_results_dir: Path):
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({"contract_hash": "abc123xyz"})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 42})
    assert result.passed is True


def test_codex_gate_required_empty_hash(config_file: Path, gate_results_dir: Path):
    gate_results_dir.joinpath("pr-42-codex_gate.json").write_text(
        json.dumps({"contract_hash": ""})
    )
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.GATE_RESULTS_DIR", gate_results_dir):
        result = enforcer.check("codex_gate_required", {"pr_number": 42})
    assert result.passed is False
    assert "empty contract_hash" in result.message


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
    missing = tmp_path / "no_audit.ndjson"
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.AUDIT_LOG", missing):
        result = enforcer._check_decision_audit_trail(
            CheckConfig(name="decision_audit_trail", level=3), {}
        )
    assert result.passed is False


def test_decision_audit_trail_has_entries(config_file: Path, tmp_path: Path):
    audit = tmp_path / "governance_audit.ndjson"
    audit.write_text('{"ts": "2026-04-13T00:00:00Z", "event": "test"}\n')
    enforcer = GovernanceEnforcer()
    enforcer.load_config(config_file)
    with patch("governance_enforcer.AUDIT_LOG", audit):
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
        result = enforcer.check("codex_gate_required", {"pr_number": 999})

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
