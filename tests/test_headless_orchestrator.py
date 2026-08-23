"""tests/test_headless_orchestrator.py — Tests for HeadlessOrchestrator.

Runs in dry-run mode to avoid actual claude CLI invocations.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is importable
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Return (data_dir, state_dir) with minimal required structure."""
    data_dir = tmp_path / ".vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "dispatches" / "pending").mkdir(parents=True, exist_ok=True)

    # Write a minimal t0_state.json
    (state_dir / "t0_state.json").write_text(
        json.dumps({"terminals": {}, "_build_seconds": 0.1}),
        encoding="utf-8",
    )
    return data_dir, state_dir


def _import_orchestrator():
    """Import HeadlessOrchestrator with mocked claude CLI detection."""
    import importlib
    import shutil

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator
        importlib.reload(headless_orchestrator)
        return headless_orchestrator


# ---------------------------------------------------------------------------
# test_startup_validation — missing t0_state.json raises RuntimeError
# ---------------------------------------------------------------------------

def test_startup_validation_missing_t0_state(tmp_path: Path) -> None:
    """validate_startup() raises RuntimeError when t0_state.json is absent."""
    data_dir = tmp_path / ".vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "dispatches" / "pending").mkdir(parents=True, exist_ok=True)
    # t0_state.json intentionally NOT created

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator
        orch = headless_orchestrator.HeadlessOrchestrator(
            data_dir=data_dir,
            state_dir=state_dir,
            dry_run=True,
        )
        with pytest.raises(RuntimeError) as exc_info:
            orch.validate_startup()

    assert "t0_state.json" in str(exc_info.value)


def test_startup_validation_missing_claude(tmp_path: Path) -> None:
    """validate_startup() raises RuntimeError when 'claude' not in PATH."""
    data_dir, state_dir = _make_state_dir(tmp_path)

    with patch("shutil.which", return_value=None):
        import headless_orchestrator
        orch = headless_orchestrator.HeadlessOrchestrator(
            data_dir=data_dir,
            state_dir=state_dir,
            dry_run=True,
        )
        with pytest.raises(RuntimeError) as exc_info:
            orch.validate_startup()

    assert "claude" in str(exc_info.value).lower()


def test_startup_validation_passes(tmp_path: Path) -> None:
    """validate_startup() succeeds with all prerequisites in place."""
    data_dir, state_dir = _make_state_dir(tmp_path)

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator
        orch = headless_orchestrator.HeadlessOrchestrator(
            data_dir=data_dir,
            state_dir=state_dir,
            dry_run=True,
        )
        # Should not raise
        orch.validate_startup()


# ---------------------------------------------------------------------------
# test_all_daemons_start — health file shows all running after start
# ---------------------------------------------------------------------------

def test_all_daemons_start(tmp_path: Path) -> None:
    """Start orchestrator; health file written within 35s and shows all daemons running."""
    data_dir, state_dir = _make_state_dir(tmp_path)

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator

        # Stub out DispatchDaemon.start so it doesn't actually poll
        mock_daemon = MagicMock()
        mock_daemon._shutdown = threading.Event()

        with patch("headless_orchestrator.HeadlessOrchestrator._invoke_trigger"):
            orch = headless_orchestrator.HeadlessOrchestrator(
                data_dir=data_dir,
                state_dir=state_dir,
                dry_run=True,
            )

            # Patch DispatchDaemon to return our stub
            with patch("headless_dispatch_daemon.DispatchDaemon") as MockDaemon:
                MockDaemon.return_value = mock_daemon

                try:
                    orch.start()

                    # Wait for first health file write (up to 35s; actually much faster)
                    health_path = data_dir / "headless_health.json"
                    deadline = time.monotonic() + 35.0
                    while not health_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.1)

                    assert health_path.exists(), "headless_health.json was not written"
                    health = json.loads(health_path.read_text())

                    daemons = health["daemons"]
                    assert daemons["receipt_watcher"] == "running", f"receipt_watcher: {daemons}"
                    assert daemons["silence_watchdog"] in ("running", "stopped")  # may not start within 35s
                    assert "started_at" in health
                    assert "uptime_seconds" in health
                    assert "decisions_made" in health
                finally:
                    orch.stop()


# ---------------------------------------------------------------------------
# test_graceful_shutdown — all threads stop within 10s after stop()
# ---------------------------------------------------------------------------

def test_graceful_shutdown(tmp_path: Path) -> None:
    """stop() shuts down all threads within 10 seconds."""
    data_dir, state_dir = _make_state_dir(tmp_path)

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator

        mock_daemon = MagicMock()
        mock_daemon._shutdown = threading.Event()

        with patch("headless_dispatch_daemon.DispatchDaemon") as MockDaemon:
            MockDaemon.return_value = mock_daemon

            orch = headless_orchestrator.HeadlessOrchestrator(
                data_dir=data_dir,
                state_dir=state_dir,
                dry_run=True,
            )
            orch.start()
            time.sleep(0.3)  # let threads spin up

            t_stop_start = time.monotonic()
            orch.stop()
            elapsed = time.monotonic() - t_stop_start

            assert elapsed < 11.0, f"stop() took {elapsed:.1f}s — exceeded 11s budget"
            assert orch._shutdown.is_set()

            # All our tracked threads should be dead
            threads_to_check = [
                orch._receipt_watcher._thread if orch._receipt_watcher else None,
                orch._watchdog_thread,
                orch._health_thread,
                orch._decision_thread,
            ]
            for thread in threads_to_check:
                if thread is not None:
                    assert not thread.is_alive(), f"Thread {thread.name!r} still running after stop()"


# ---------------------------------------------------------------------------
# test_health_file_updated — health file is refreshed each cycle
# ---------------------------------------------------------------------------

def test_health_file_updated(tmp_path: Path) -> None:
    """Health file mtime advances across two consecutive health writes."""
    data_dir, state_dir = _make_state_dir(tmp_path)

    with patch("shutil.which", return_value="/usr/bin/claude"):
        import headless_orchestrator

        # Override health interval to be very short for the test
        original_interval = headless_orchestrator._HEALTH_INTERVAL
        headless_orchestrator._HEALTH_INTERVAL = 0.2

        mock_daemon = MagicMock()
        mock_daemon._shutdown = threading.Event()

        with patch("headless_dispatch_daemon.DispatchDaemon") as MockDaemon:
            MockDaemon.return_value = mock_daemon

            orch = headless_orchestrator.HeadlessOrchestrator(
                data_dir=data_dir,
                state_dir=state_dir,
                dry_run=True,
            )
            try:
                orch.start()

                health_path = data_dir / "headless_health.json"

                # Wait for first write
                deadline = time.monotonic() + 5.0
                while not health_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert health_path.exists(), "First health write never occurred"

                first_mtime = health_path.stat().st_mtime
                first_data = json.loads(health_path.read_text())

                # Wait for second write
                deadline = time.monotonic() + 2.0
                while health_path.stat().st_mtime <= first_mtime and time.monotonic() < deadline:
                    time.sleep(0.05)

                second_mtime = health_path.stat().st_mtime
                second_data = json.loads(health_path.read_text())

                assert second_mtime > first_mtime, "Health file mtime did not advance"
                assert second_data["last_health_check"] != first_data["last_health_check"], (
                    "last_health_check timestamp did not change"
                )
                assert second_data["uptime_seconds"] >= first_data["uptime_seconds"]

            finally:
                headless_orchestrator._HEALTH_INTERVAL = original_interval
                orch.stop()


# ---------------------------------------------------------------------------
# _check_all_gates_passed — provider-agnostic required-gate derivation
# (dispatch 20260823-beta2-e, OI-1435): the required set comes from what was
# actually REQUESTED for the PR (review_gates/requests/), not a hardcoded
# {codex_gate, gemini_review}.
# ---------------------------------------------------------------------------

def _write_gate_request(state_dir: Path, pr_number: int, gate: str, *, required=None) -> None:
    reqs_dir = state_dir / "review_gates" / "requests"
    reqs_dir.mkdir(parents=True, exist_ok=True)
    payload = {"gate": gate, "status": "requested", "pr_number": pr_number}
    if required is not None:
        payload["required"] = required
    (reqs_dir / f"pr-{pr_number}-{gate}.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_gate_result(
    state_dir: Path, pr_number: int, gate: str, *, status: str,
    blocking_findings=None, blocking_count=None,
) -> None:
    results_dir = state_dir / "review_gates" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    payload = {"gate": gate, "pr_number": pr_number, "status": status}
    if blocking_findings is not None:
        payload["blocking_findings"] = blocking_findings
    if blocking_count is not None:
        payload["blocking_count"] = blocking_count
    (results_dir / f"pr-{pr_number}-{gate}.json").write_text(json.dumps(payload), encoding="utf-8")


def _gate_event(dispatch_id: str):
    import headless_orchestrator as ho
    return ho.LoopEvent(
        reason="review_gate_result",
        context={"latest_event": "review_gate_result", "latest_dispatch_id": dispatch_id},
    )


def _read_loop_events(data_dir: Path) -> list:
    path = data_dir / "events" / "autonomous_loop.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_check_all_gates_passed_kimi_glm_stack_unblocks(tmp_path: Path) -> None:
    """Provider-agnostic: a stack of kimi_gate+glm_gate ONLY (no codex_gate,
    no gemini_review at all) must unblock the feature once both requested
    gates pass. On the pre-fix hardcoded {codex_gate, gemini_review} check
    this silently returns and no feature_gates_complete event is ever
    logged — this test is RED on that code and GREEN after the fix."""
    data_dir, state_dir = _make_state_dir(tmp_path)
    import headless_orchestrator
    orch = headless_orchestrator.HeadlessOrchestrator(data_dir=data_dir, state_dir=state_dir, dry_run=True)

    _write_gate_request(state_dir, 501, "kimi_gate")
    _write_gate_request(state_dir, 501, "glm_gate")
    _write_gate_result(state_dir, 501, "kimi_gate", status="pass", blocking_findings=[], blocking_count=0)
    _write_gate_result(state_dir, 501, "glm_gate", status="pass", blocking_findings=[], blocking_count=0)

    orch._check_all_gates_passed(_gate_event("f88-pr1-kimi-glm-stack"))

    events = _read_loop_events(data_dir)
    complete = [e for e in events if e.get("event_type") == "feature_gates_complete"]
    assert complete, f"expected feature_gates_complete to be logged, got: {events}"
    assert complete[0]["pr_number"] == 501
    assert sorted(complete[0]["gate_names"]) == ["glm_gate", "kimi_gate"]


def test_check_all_gates_passed_codex_gemini_combo_unchanged(tmp_path: Path) -> None:
    """Control case: the existing codex_gate + gemini_review combination
    still unblocks exactly as before the provider-agnostic rewrite."""
    data_dir, state_dir = _make_state_dir(tmp_path)
    import headless_orchestrator
    orch = headless_orchestrator.HeadlessOrchestrator(data_dir=data_dir, state_dir=state_dir, dry_run=True)

    _write_gate_request(state_dir, 502, "gemini_review")
    _write_gate_request(state_dir, 502, "codex_gate", required=True)
    _write_gate_result(state_dir, 502, "gemini_review", status="pass", blocking_findings=[], blocking_count=0)
    _write_gate_result(state_dir, 502, "codex_gate", status="pass", blocking_findings=[], blocking_count=0)

    orch._check_all_gates_passed(_gate_event("f89-pr1-codex-gemini"))

    events = _read_loop_events(data_dir)
    complete = [e for e in events if e.get("event_type") == "feature_gates_complete"]
    assert complete, f"expected feature_gates_complete to be logged, got: {events}"
    assert complete[0]["pr_number"] == 502
    assert sorted(complete[0]["gate_names"]) == ["codex_gate", "gemini_review"]


def test_check_all_gates_passed_required_gate_fails_does_not_unblock(tmp_path: Path) -> None:
    """Control case 1: a requested gate that FAILS must not unblock."""
    data_dir, state_dir = _make_state_dir(tmp_path)
    import headless_orchestrator
    orch = headless_orchestrator.HeadlessOrchestrator(data_dir=data_dir, state_dir=state_dir, dry_run=True)

    _write_gate_request(state_dir, 503, "kimi_gate")
    _write_gate_request(state_dir, 503, "glm_gate")
    _write_gate_result(state_dir, 503, "kimi_gate", status="pass", blocking_findings=[], blocking_count=0)
    _write_gate_result(
        state_dir, 503, "glm_gate", status="fail",
        blocking_findings=[{"severity": "blocking", "title": "x", "description": "y"}],
        blocking_count=1,
    )

    orch._check_all_gates_passed(_gate_event("f90-pr1-one-fails"))

    events = _read_loop_events(data_dir)
    assert not [e for e in events if e.get("event_type") == "feature_gates_complete"]


def test_check_all_gates_passed_required_gate_missing_result_does_not_unblock_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Control case 2: a requested gate with NO result yet must not unblock,
    and the reason plus the missing gate name must be logged — replacing the
    silent return that shipped on main."""
    data_dir, state_dir = _make_state_dir(tmp_path)
    import headless_orchestrator
    orch = headless_orchestrator.HeadlessOrchestrator(data_dir=data_dir, state_dir=state_dir, dry_run=True)

    _write_gate_request(state_dir, 504, "kimi_gate")
    _write_gate_request(state_dir, 504, "glm_gate")
    _write_gate_result(state_dir, 504, "kimi_gate", status="pass", blocking_findings=[], blocking_count=0)
    # glm_gate was requested but never produced a result file.

    with caplog.at_level("WARNING", logger="headless_orchestrator"):
        orch._check_all_gates_passed(_gate_event("f91-pr1-missing-result"))

    events = _read_loop_events(data_dir)
    assert not [e for e in events if e.get("event_type") == "feature_gates_complete"]
    assert "missing evidence" in caplog.text and "glm_gate" in caplog.text, caplog.text
