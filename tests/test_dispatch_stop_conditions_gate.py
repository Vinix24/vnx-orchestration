"""test_dispatch_stop_conditions_gate.py — golf 2A
(dispatch/20260904-deur-leest-stopcondities).

stop_conditions.py (PR #1754) measures four autonomous-chain stop-conditions
(E1 main-CI-red, E4 gh-auth-dead, provider-exhaustion, E6
repeated-gate-failure-cause) but nothing read its output — the door fired
blind. This dispatch wires the door to measure stop_conditions.run_all_checks()
LIVE before every fire (both real and dry-run, via build_runtime_snapshot) and
refuse fail-closed on any CheckStatus.TRIGGERED result, with an explicit
--override-stop-conditions REASON escape hatch (mirrors --refire's shape: a
per-fire reason, logged, re-required on the next fire).

Every TRIGGERED-blocking test in this file is RED on the branch point: before
this fix, build_runtime_snapshot never called stop_conditions at all, so a
dispatch fired straight through regardless of any active stop-condition. Ran
red against the pre-fix revision (git stash of the dispatch_cli.py changes),
green after — see the dispatch report for the exact counts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_cli import run_dispatch  # noqa: E402
from stop_conditions import CheckStatus, StopConditionResult  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_dispatch_refire_guard.py's _make_bundle)
# ---------------------------------------------------------------------------

def _make_bundle(tmp_path: Path, *, staging_id: str, dispatch_id: str) -> "tuple[Path, Path]":
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(instruction),
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": "codex_gate",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
        "force_tmux": True,
        "force_tmux_reason": "stop-conditions gate test asserts door decisions, not lane behavior",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _result(check_id: str, status: CheckStatus, message: str = "test") -> StopConditionResult:
    return StopConditionResult(check_id=check_id, status=status, message=message)


# ---------------------------------------------------------------------------
# Unit-level: mocked stop_conditions.run_all_checks, one scenario each.
# ---------------------------------------------------------------------------

def test_triggered_stop_condition_blocks_fire(tmp_path, monkeypatch, capsys):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-triggered", dispatch_id="20260904-stopcond-triggered",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    fake_results = [
        _result("main_ci_red", CheckStatus.CLEAR),
        _result("gh_auth_dead", CheckStatus.CLEAR),
        _result("provider_exhausted", CheckStatus.TRIGGERED, "kimi: 3 consecutive auth_rejected"),
        _result("repeated_gate_failure_cause", CheckStatus.CLEAR),
    ]

    with patch("stop_conditions.run_all_checks", return_value=fake_results) as mock_run, \
         patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "an active TRIGGERED stop-condition must refuse the fire"
    mock_execute.assert_not_called()
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs.get("write_halt") is True, (
        "the door's own measurement must persist halt.json (write_halt=True) "
        "so other readers (daemons/dashboards) share the same durable record"
    )
    err = capsys.readouterr().err
    assert "stop-conditions-triggered" in err
    assert "provider_exhausted" in err


def test_triggered_stop_condition_with_override_reason_proceeds(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-override", dispatch_id="20260904-stopcond-override",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    fake_results = [_result("main_ci_red", CheckStatus.TRIGGERED, "VNX CI conclusion=failure on main")]

    with patch("stop_conditions.run_all_checks", return_value=fake_results), \
         patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(
            spec_file,
            stop_conditions_override_reason="operator: known flaky CI job, verified main is green by hand",
        )

    assert rc == 0, "an explicit --override-stop-conditions reason must let the dispatch proceed"
    mock_execute.assert_called_once()


def test_unmeasurable_stop_condition_does_not_block(tmp_path, monkeypatch, capsys):
    """UNMEASURABLE is not a green light, but it is also not grounds to
    refuse — a caller that refused on UNMEASURABLE would close the door
    every time gh hiccups, which is fragility, not fail-closed."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-unmeasurable", dispatch_id="20260904-stopcond-unmeasurable",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    fake_results = [
        _result("main_ci_red", CheckStatus.UNMEASURABLE, "gh CLI niet beschikbaar"),
        _result("gh_auth_dead", CheckStatus.UNMEASURABLE, "gh CLI niet beschikbaar"),
        _result("provider_exhausted", CheckStatus.UNMEASURABLE, "receipts-bestand ontbreekt"),
        _result("repeated_gate_failure_cause", CheckStatus.UNMEASURABLE, "resultaten-map ontbreekt"),
    ]

    with patch("stop_conditions.run_all_checks", return_value=fake_results), \
         patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0, "all-UNMEASURABLE must not block — unmeasurable is not evidence of a real condition"
    mock_execute.assert_called_once()
    err = capsys.readouterr().err
    assert "unmeasurable" in err.lower()


def test_all_clear_does_not_block(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-clear", dispatch_id="20260904-stopcond-clear",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    fake_results = [
        _result("main_ci_red", CheckStatus.CLEAR),
        _result("gh_auth_dead", CheckStatus.CLEAR),
        _result("provider_exhausted", CheckStatus.CLEAR),
        _result("repeated_gate_failure_cause", CheckStatus.CLEAR),
    ]

    with patch("stop_conditions.run_all_checks", return_value=fake_results), \
         patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_execute.assert_called_once()


def test_stop_conditions_checked_on_dry_run_too(tmp_path, monkeypatch, capsys):
    """_check_reachability's own precedent (OI-1248): a lane-blocking fact
    refuses on BOTH the dry-run and the real path. Stop-conditions follow the
    same rule — a dry-run must preview the real refusal, not fire blind."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-dryrun", dispatch_id="20260904-stopcond-dryrun",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    fake_results = [_result("gh_auth_dead", CheckStatus.TRIGGERED, "gh is niet geauthenticeerd")]

    with patch("stop_conditions.run_all_checks", return_value=fake_results), \
         patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 1, "dry-run must also refuse on a TRIGGERED stop-condition"
    mock_execute.assert_not_called()


def test_measurement_crash_degrades_to_unmeasurable_not_a_door_crash(tmp_path, monkeypatch):
    """A bug in the checker must never become an outage of the whole door —
    fail-open on the CHECKER's own crash, never a raised exception out of
    run_dispatch."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-crash", dispatch_id="20260904-stopcond-crash",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("stop_conditions.run_all_checks", side_effect=RuntimeError("boom")), \
         patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_execute.assert_called_once()


# ---------------------------------------------------------------------------
# End-to-end: REAL stop_conditions.check_provider_exhausted (no mocking of
# stop_conditions itself) against a crafted receipts ledger reproducing the
# exact live scenario named in the dispatch brief — three consecutive kimi
# auth_rejected receipts. gh-dependent checks (main_ci_red, gh_auth_dead) are
# neutralized via shutil.which so the test has no real network/gh-auth
# dependency; provider_exhausted and repeated_gate_failure_cause run for real.
# ---------------------------------------------------------------------------

def test_real_provider_exhaustion_blocks_any_provider_end_to_end(tmp_path, monkeypatch, capsys):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-e2e", dispatch_id="20260904-stopcond-e2e",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as fh:
        for i in range(3):
            rec = {
                "event_type": "task_complete",
                "provider": "kimi",
                "status": "failed",
                "failure_class": "auth_rejected",
                "dispatch_id": f"20260903-kimi-fail-{i}",
                "timestamp": f"2026-09-03T0{i}:00:00Z",
            }
            fh.write(json.dumps(rec) + "\n")

    import stop_conditions
    monkeypatch.setattr(stop_conditions.shutil, "which", lambda _bin: None)

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, (
        "3 consecutive kimi auth_rejected receipts must trip "
        "check_provider_exhausted for real and refuse the fire — even though "
        "THIS dispatch targets claude/T0, not kimi: it is a chain-level halt"
    )
    mock_execute.assert_not_called()
    err = capsys.readouterr().err
    assert "stop-conditions-triggered" in err
    assert "provider_exhausted" in err
    assert (state_dir / "halt.json").exists(), (
        "the live measurement must persist halt.json as a side effect "
        "(write_halt=True) for other readers"
    )
    halt = json.loads((state_dir / "halt.json").read_text(encoding="utf-8"))
    assert any(t["check_id"] == "provider_exhausted" for t in halt["triggered"])
