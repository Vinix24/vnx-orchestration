"""tests/test_gate_status_schema.py — CFX-3 gate result schema canonicalization.

Verifies that scripts/lib/gate_status.is_pass() interprets the canonical
``status`` field correctly across every shape that real writers in this
repo produce, plus the legacy ``verdict`` fallback for graceful migration.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from gate_status import (
    FAIL_STATES,
    INCOMPLETE_STATES,
    PASS_STATES,
    canonical_status,
    is_pass,
    is_terminal,
)


# ---------------------------------------------------------------------------
# Cases A-G: canonical decision matrix
# ---------------------------------------------------------------------------


def test_case_a_status_approve_no_findings_passes() -> None:
    result = {"status": "approve", "blocking_findings": [], "blocking_count": 0}
    passed, reason = is_pass(result)
    assert passed is True
    assert reason == "passed"


def test_case_b_status_completed_no_findings_passes() -> None:
    result = {"status": "completed", "blocking_findings": [], "blocking_count": 0}
    passed, reason = is_pass(result)
    assert passed is True
    assert reason == "passed"


def test_case_c_status_failed_fails() -> None:
    result = {"status": "failed"}
    passed, reason = is_pass(result)
    assert passed is False
    assert "failed" in reason


def test_case_d_status_approve_with_blocking_fails() -> None:
    result = {
        "status": "approve",
        "blocking_findings": [{"severity": "error"}, {"severity": "error"}],
        "blocking_count": 2,
    }
    passed, reason = is_pass(result)
    assert passed is False
    assert "blocking" in reason


def test_case_e_status_running_fails_as_incomplete() -> None:
    result = {"status": "running"}
    passed, reason = is_pass(result)
    assert passed is False
    assert "incomplete" in reason


def test_case_f_legacy_verdict_pass_uses_fallback() -> None:
    """Legacy file: verdict=pass, status absent → must still be honored.

    Migration must be graceful — old files on disk are not rewritten.
    A DeprecationWarning is emitted to discourage new writers from this shape.
    """
    result = {"verdict": "pass", "blocking_findings": [], "blocking_count": 0}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        passed, reason = is_pass(result)
    assert passed is True
    assert reason == "passed"
    assert any(issubclass(w.category, DeprecationWarning) for w in caught), (
        "expected DeprecationWarning for legacy verdict fallback"
    )


def test_case_g_no_status_no_verdict_fails_unknown() -> None:
    result: Dict[str, Any] = {"summary": "nothing here"}
    passed, reason = is_pass(result)
    assert passed is False
    assert "no status" in reason or "unknown" in reason


# ---------------------------------------------------------------------------
# Edge cases the canonical sets must cover
# ---------------------------------------------------------------------------


def test_status_pending_treated_as_incomplete_not_pass() -> None:
    result = {"status": "pending"}
    passed, reason = is_pass(result)
    assert passed is False
    assert "incomplete" in reason


def test_status_blocked_treated_as_fail() -> None:
    result = {"status": "blocked"}
    passed, _ = is_pass(result)
    assert passed is False


def test_status_pass_with_zero_blocking_count_passes() -> None:
    result = {"status": "pass", "blocking_count": 0}
    passed, _ = is_pass(result)
    assert passed is True


def test_status_completed_with_blocking_count_three_fails() -> None:
    result = {"status": "completed", "blocking_count": 3}
    passed, reason = is_pass(result)
    assert passed is False
    assert "blocking_count" in reason or "blocking" in reason


def test_blocking_findings_list_overrides_pass_status() -> None:
    result = {"status": "approve", "blocking_findings": [{"sev": "error"}]}
    passed, _ = is_pass(result)
    assert passed is False


def test_canonical_status_lowercases_and_trims() -> None:
    assert canonical_status({"status": "APPROVE"}) == "approve"
    assert canonical_status({"status": ""}) == ""
    assert canonical_status({}) == ""
    assert canonical_status({"verdict": "PASS"}) == "pass"


def test_is_terminal_distinguishes_terminal_vs_inflight() -> None:
    assert is_terminal({"status": "approve"}) is True
    assert is_terminal({"status": "completed"}) is True
    assert is_terminal({"status": "failed"}) is True
    assert is_terminal({"status": "not_executable"}) is True
    assert is_terminal({"status": "pending"}) is False
    assert is_terminal({"status": "running"}) is False
    assert is_terminal({}) is False
    # Legacy verdict fallback
    assert is_terminal({"verdict": "pass"}) is True


def test_pass_states_disjoint_from_fail_and_incomplete() -> None:
    """Canonical sets must not overlap — no status string can be both."""
    assert PASS_STATES.isdisjoint(FAIL_STATES)
    assert PASS_STATES.isdisjoint(INCOMPLETE_STATES)
    assert FAIL_STATES.isdisjoint(INCOMPLETE_STATES)


# ---------------------------------------------------------------------------
# Case H: schema validator — every writer's output passes is_pass parse
# ---------------------------------------------------------------------------


def _writer_output_gate_artifacts() -> Dict[str, Any]:
    """Shape produced by scripts/lib/gate_artifacts.py:materialize_artifacts."""
    return {
        "gate": "codex_gate",
        "pr_id": "100",
        "pr_number": 100,
        "status": "completed",
        "summary": "codex_gate execution completed successfully",
        "contract_hash": "abc123",
        "report_path": "/tmp/report.md",
        "findings": [],
        "blocking_findings": [],
        "advisory_findings": [],
        "required_reruns": [],
        "residual_risk": "",
        "duration_seconds": 12.3,
        "recorded_at": "2026-04-29T00:00:00Z",
    }


def _writer_output_gate_recorder_failure() -> Dict[str, Any]:
    """Shape produced by scripts/lib/gate_recorder.py:record_failure."""
    return {
        "gate": "codex_gate",
        "pr_id": "100",
        "pr_number": 100,
        "status": "failed",
        "reason": "timeout",
        "reason_detail": "Gate exceeded 600s",
        "duration_seconds": 600.0,
        "partial_output_lines": 0,
        "runner_pid": 1234,
        "killed_at": "2026-04-29T00:00:00Z",
        "summary": "Gate execution timeout: Gate exceeded 600s",
        "contract_hash": "abc123",
        "report_path": "",
        "blocking_findings": [],
        "advisory_findings": [],
        "required_reruns": ["codex_gate"],
        "residual_risk": "Gate timeout. Re-run required.",
        "recorded_at": "2026-04-29T00:00:00Z",
    }


def _writer_output_gate_recorder_not_executable() -> Dict[str, Any]:
    """Shape produced by scripts/lib/gate_recorder.py:record_not_executable."""
    return {
        "gate": "codex_gate",
        "pr_id": "100",
        "pr_number": 100,
        "status": "not_executable",
        "reason": "provider_disabled",
        "reason_detail": "VNX_CODEX_HEADLESS_ENABLED=0",
        "summary": "codex_gate not executable: VNX_CODEX_HEADLESS_ENABLED=0",
        "contract_hash": "abc123",
        "report_path": "",
        "blocking_findings": [],
        "advisory_findings": [],
        "required_reruns": [],
        "residual_risk": "Gate evidence not available.",
        "recorded_at": "2026-04-29T00:00:00Z",
    }


def _writer_output_gate_result_parser_approve() -> Dict[str, Any]:
    """Shape produced by scripts/lib/gate_result_parser.py:record_result on approve."""
    return {
        "gate": "gemini_review",
        "pr_number": 100,
        "pr_id": "100",
        "branch": "feat/x",
        "status": "approve",
        "summary": "approved",
        "findings": [],
        "advisory_findings": [],
        "blocking_findings": [],
        "advisory_count": 0,
        "blocking_count": 0,
        "residual_risk": "",
        "contract_hash": "abc123",
        "report_path": "/tmp/report.md",
        "required_reruns": [],
        "recorded_at": "2026-04-29T00:00:00Z",
    }


@pytest.mark.parametrize(
    "writer,expected_pass",
    [
        (_writer_output_gate_artifacts, True),
        (_writer_output_gate_recorder_failure, False),
        (_writer_output_gate_recorder_not_executable, False),
        (_writer_output_gate_result_parser_approve, True),
    ],
)
def test_every_writer_output_parses_via_is_pass(writer, expected_pass) -> None:
    """Schema H: each writer's output must be classifiable by is_pass."""
    result = writer()
    passed, reason = is_pass(result)
    assert passed is expected_pass, (
        f"writer {writer.__name__}: expected pass={expected_pass}, got {passed} ({reason})"
    )
    # And it must be JSON-roundtrippable (real writers persist via json.dumps)
    roundtripped = json.loads(json.dumps(result))
    passed2, _ = is_pass(roundtripped)
    assert passed2 is expected_pass


# ---------------------------------------------------------------------------
# Real-disk regression: existing approve/completed files in this repo's
# .vnx-data/ must classify correctly. Skipped when the dir is unavailable
# (e.g., CI without runtime state).
# ---------------------------------------------------------------------------


_REAL_RESULTS_DIR = (
    Path(__file__).resolve().parent.parent
    / ".vnx-data"
    / "state"
    / "review_gates"
    / "results"
)


@pytest.mark.skipif(not _REAL_RESULTS_DIR.is_dir(), reason="no runtime state present")
def test_real_disk_results_classify_without_unknown_status() -> None:
    """Every real result file on disk must produce a known classification.

    Catches the original CFX-3 bug: closure verifier returning false-negative
    for status="completed"/"approve" because it only matched verdict=="pass".
    """
    files = list(_REAL_RESULTS_DIR.glob("*.json"))
    if not files:
        pytest.skip("no result files present")
    unknown: list[str] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        _passed, reason = is_pass(data)
        if "unknown status" in reason:
            unknown.append(f"{path.name}: {reason}")
    assert not unknown, "results with unknown status:\n" + "\n".join(unknown[:10])


# ---------------------------------------------------------------------------
# Integration: closure_verifier reader path uses is_pass()
# ---------------------------------------------------------------------------


def test_closure_verifier_reads_status_completed_as_pass(tmp_path: Path) -> None:
    """The original CFX-3 bug: gate_artifacts writes status=completed but
    closure_verifier required verdict==pass. This regression test asserts
    the closure verifier reader path now accepts status=completed.
    """
    import closure_verifier  # noqa: WPS433
    from review_contract import ReviewContract, Deliverable

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    report = tmp_path / "report.md"
    report.write_text("dummy", encoding="utf-8")

    payload = {
        "gate": "codex_gate",
        "pr_id": "PR-99",
        "status": "completed",
        "blocking_findings": [],
        "blocking_count": 0,
        "report_path": str(report),
        "contract_hash": "deadbeef",
    }
    (results_dir / "pr99-codex_gate-contract.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    contract = ReviewContract(
        pr_id="PR-99",
        pr_title="x",
        feature_title="f",
        branch="b",
        track="A",
        risk_class="high",
        merge_policy="standard",
        closure_stage="ready",
        deliverables=[Deliverable(category="impl", description="d")],
        review_stack=["codex_gate"],
        content_hash="deadbeef",
    )

    checks = closure_verifier._validate_review_evidence(contract, results_dir)
    gate_check = next(c for c in checks if c.name == "gate_codex_gate")
    assert gate_check.status == "PASS", (
        f"closure_verifier must accept status=completed as pass, got {gate_check.status}: {gate_check.detail}"
    )
