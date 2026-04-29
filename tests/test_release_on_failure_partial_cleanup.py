"""Regression tests for Finding 1: rc_release_on_failure cleanup_complete handling.

Verifies that when failure_recorded=False but lease_released=True (i.e. cleanup_complete=False),
the shell wrapper does NOT suppress the partial failure as a clean success. Both the Python
contract (runtime_core.py) and the shell audit emission are covered.

Shell-side contract (dispatch_lifecycle.sh rc_release_on_failure):
  - cleanup_complete=False AND lease_released=True → failure_recording_missed is logged
    AND audit entry includes error field "broker_failure_not_recorded"
  - cleanup_complete=True AND lease_released=True  → clean success, no error in audit
  - lease_released=False                           → lease_release_failed, error in audit
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = PROJECT_ROOT / "scripts" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from runtime_core import RuntimeCore
from dispatch_broker import DispatchBroker
from lease_manager import LeaseManager
from runtime_coordination import init_schema


# ---------------------------------------------------------------------------
# Python-layer contract: cleanup_complete is False when failure_recorded=False
# ---------------------------------------------------------------------------

def _make_core(tmp_path: Path):
    state_dir = tmp_path / "state"
    dispatch_dir = tmp_path / "dispatches"
    state_dir.mkdir(parents=True)
    dispatch_dir.mkdir(parents=True)
    init_schema(state_dir)
    broker = DispatchBroker(str(state_dir), str(dispatch_dir), shadow_mode=False)
    lease_mgr = LeaseManager(state_dir, auto_init=False)
    return RuntimeCore(broker=broker, lease_mgr=lease_mgr), broker, lease_mgr


class TestCleanupCompleteContract:
    """Verify runtime_core.py sets cleanup_complete correctly — the value the shell reads."""

    def test_cleanup_complete_false_when_failure_recorded_false(self, tmp_path):
        """When broker.deliver_failure raises, failure_recorded=False → cleanup_complete=False."""
        core, broker, lease_mgr = _make_core(tmp_path)
        broker.register("partial-001", "Work", terminal_id="T1")
        lease_result = lease_mgr.acquire("T1", dispatch_id="partial-001")
        generation = lease_result.generation
        delivery = core.delivery_start("partial-001", "T1")
        attempt_id = delivery.attempt_id or ""

        original = broker.deliver_failure

        def _raise(*a, **kw):
            raise RuntimeError("simulated broker error")

        broker.deliver_failure = _raise
        try:
            result = core.release_on_delivery_failure(
                "partial-001", attempt_id, "T1", generation, "tmux failed"
            )
        finally:
            broker.deliver_failure = original

        assert result["failure_recorded"] is False
        assert result["lease_released"] is True
        assert result["cleanup_complete"] is False, (
            "cleanup_complete must be False when failure_recorded is False"
        )
        assert result["failure_error"] is not None

    def test_cleanup_complete_true_when_both_succeed(self, tmp_path):
        """Normal path: both steps succeed → cleanup_complete=True."""
        core, broker, lease_mgr = _make_core(tmp_path)
        broker.register("full-001", "Work", terminal_id="T2")
        lease_result = lease_mgr.acquire("T2", dispatch_id="full-001")
        generation = lease_result.generation
        delivery = core.delivery_start("full-001", "T2")

        result = core.release_on_delivery_failure(
            "full-001", delivery.attempt_id or "", "T2", generation, "tmux failed"
        )

        assert result["failure_recorded"] is True
        assert result["lease_released"] is True
        assert result["cleanup_complete"] is True

    def test_cleanup_complete_false_when_lease_release_fails(self, tmp_path):
        """When lease release fails, cleanup_complete=False regardless of failure_recorded."""
        core, broker, lease_mgr = _make_core(tmp_path)
        broker.register("lease-fail-001", "Work", terminal_id="T1")
        lease_result = lease_mgr.acquire("T1", dispatch_id="lease-fail-001")
        generation = lease_result.generation
        delivery = core.delivery_start("lease-fail-001", "T1")

        stale_generation = generation - 1
        result = core.release_on_delivery_failure(
            "lease-fail-001", delivery.attempt_id or "", "T1",
            stale_generation, "tmux failed"
        )

        assert result["lease_released"] is False
        assert result["cleanup_complete"] is False


# ---------------------------------------------------------------------------
# Shell-layer contract: rc_release_on_failure uses cleanup_complete correctly
# ---------------------------------------------------------------------------

def _run_shell_rc_release_on_failure(
    tmpdir: Path,
    mock_json: dict,
) -> tuple[str, str]:
    """Run a minimal bash snippet that exercises rc_release_on_failure with a mocked
    _rc_python returning the given JSON. Returns (failures_log_text, audit_ndjson_text)."""
    mock_json_str = json.dumps(mock_json)
    failures_log = tmpdir / "failures.log"
    audit_ndjson = tmpdir / "audit.ndjson"

    script = textwrap.dedent(f"""\
        #!/bin/bash
        set -uo pipefail

        # Stub every external dependency before sourcing the library
        log() {{ true; }}
        log_structured_failure() {{
            echo "$1" >> "{failures_log}"
        }}
        emit_lease_cleanup_audit() {{
            local event="$3" released="$4" err="${{5:-}}"
            echo "{{\"event_type\":\"$event\",\"lease_released\":\"$released\",\"error\":\"$err\"}}" >> "{audit_ndjson}"
        }}
        rc_release_lease() {{ true; }}
        _rc_enabled() {{ return 0; }}
        _rc_python() {{
            echo '{mock_json_str}'
            return 0
        }}

        # Source only the rc_release_on_failure function — avoid sourcing unresolvable deps
        eval "$(grep -A 60 '^rc_release_on_failure()' \\
            "{PROJECT_ROOT}/scripts/lib/dispatch_lifecycle.sh" \\
            | head -60)"

        rc_release_on_failure "test-dispatch" "attempt-001" "T1" "5" "test reason"
    """)

    subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    failures = failures_log.read_text() if failures_log.exists() else ""
    audit = audit_ndjson.read_text() if audit_ndjson.exists() else ""
    return failures, audit


class TestShellCleanupCompleteHandling:
    """Shell rc_release_on_failure must surface partial failure, not report clean success."""

    def test_partial_failure_emits_failure_recording_missed(self, tmp_path):
        """When cleanup_complete=false but lease_released=true, shell must log failure_recording_missed."""
        failures, audit = _run_shell_rc_release_on_failure(
            tmp_path,
            {"failure_recorded": False, "lease_released": True,
             "cleanup_complete": False, "lease_error": None},
        )
        assert "failure_recording_missed" in failures, (
            f"Expected failure_recording_missed in structured failures. Got: {failures!r}\n"
            f"Audit: {audit!r}"
        )

    def test_partial_failure_sets_error_in_audit(self, tmp_path):
        """Audit entry for partial failure must carry broker_failure_not_recorded in error field."""
        failures, audit = _run_shell_rc_release_on_failure(
            tmp_path,
            {"failure_recorded": False, "lease_released": True,
             "cleanup_complete": False, "lease_error": None},
        )
        assert "broker_failure_not_recorded" in audit, (
            f"Expected broker_failure_not_recorded in audit error field. Got: {audit!r}"
        )

    def test_full_success_emits_no_failure(self, tmp_path):
        """When cleanup_complete=true, no failure_recording_missed should be logged."""
        failures, audit = _run_shell_rc_release_on_failure(
            tmp_path,
            {"failure_recorded": True, "lease_released": True,
             "cleanup_complete": True, "lease_error": None},
        )
        assert "failure_recording_missed" not in failures, (
            f"Unexpected failure_recording_missed for full-success path. Got: {failures!r}"
        )
        assert "broker_failure_not_recorded" not in audit, (
            f"Unexpected error in audit for full-success path. Got: {audit!r}"
        )

    def test_lease_release_failure_emits_lease_release_failed(self, tmp_path):
        """When lease_released=false, shell must log lease_release_failed."""
        failures, audit = _run_shell_rc_release_on_failure(
            tmp_path,
            {"failure_recorded": True, "lease_released": False,
             "cleanup_complete": False, "lease_error": "stale generation"},
        )
        assert "lease_release_failed" in failures, (
            f"Expected lease_release_failed for lease-not-released path. Got: {failures!r}"
        )
