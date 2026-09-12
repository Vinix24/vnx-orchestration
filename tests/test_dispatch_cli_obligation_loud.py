"""test_dispatch_cli_obligation_loud.py — dispatch-20260906-a3-obligation-loud / OI-1617:
the door must not silently fire when a gate-bearing dispatch's review-gate obligation
cannot be registered.

Root cause under test (main f6bb65df): ``dispatch_cli._register_gate_obligation`` caught
EVERY exception from ``gate_obligations.register_obligation`` — including the gate-bearing
route — and called ``_record_bookkeeping_failure`` (a fact-recording, never-raise contract),
then returned normally. The fire proceeded with no obligation on record. That is the exact
2026-09-03 incident (OI-1617): the obligation failed to register, the gate later PASSed on
real evidence, and the door still answered "NOT READY — no review-gate obligation declared"
with zero trace of why.

The fix has two parts, each pinned by tests here:

  1. The gate-bearing route (``spec.gate`` non-empty) is no longer best-effort:
     ``_register_gate_obligation`` returns a BLOCKING ``ConstraintVerdict`` (code
     ``gate-obligation-registration-failed``) when ``register_obligation`` raises, and
     ``run_dispatch`` refuses the fire — no adapter/worker is ever invoked. RED on
     main: the fire proceeds anyway (see ``TestGateBearingRegistrationFailureRefusesFire``).
  2. The no-gate route (read-only dispatch, ``spec.gate`` empty) keeps its original
     best-effort contract — a failure there is still just a recorded fact, never a
     refusal (``TestNoGateRouteStaysBestEffort``).

A third class (``TestRecordBookkeepingFailureReachesStderrAndReceiptLedger``) pins the
companion fix to ``_record_bookkeeping_failure`` itself: a bookkeeping failure now writes
a literal line to stderr (not only through the logger, which has no handler on this
process and is therefore an unreliable stderr sink) and appends a ``door_bookkeeping_failed``
event to ``t0_receipts.ndjson`` (the actual receipt ledger every receipt reader scans), in
addition to the existing ``dispatch_register.ndjson`` write. RED on main: neither the direct
stderr line nor the receipt-ledger event exist.

Fixture pattern (bundle layout, headless lane) mirrors
``tests/test_gate_obligations.py`` — the existing, working door-obligation test suite —
rather than inventing a new one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_cli
import gate_obligations
from dispatch_cli import run_dispatch


# ---------------------------------------------------------------------------
# Shared fixtures (mirrors tests/test_gate_obligations.py's _make_bundle)
# ---------------------------------------------------------------------------


def _make_bundle(
    tmp_path: Path,
    *,
    staging_id: str,
    dispatch_id: str,
    gate: str,
    dispatch_paths: "list[str] | None" = None,
    role: str = "backend-developer",
) -> "tuple[Path, Path]":
    """A promoted-style staged bundle (spec + instruction inside the bundle dir).

    The spec keeps ``force_tmux`` unset, so it routes through the default
    ``claude_headless`` lane (``_execute_claude_headless``). These tests assert
    on door-level gate-obligation state, not on lane execution, so they mock
    ``_execute_claude_headless`` — tmp_path is not a real git repo, and the
    headless lane's worktree creation correctly hard-aborts on a non-git cwd.
    """
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
        "role": role,
        "target_slot": "T0",
        "gate": gate,
        "dispatch_paths": [{"path": p} for p in (dispatch_paths or [])],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    return state_dir


def _read_ndjson(path: Path) -> "list[dict]":
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Part 1: gate-bearing registration failure refuses the fire.
# ---------------------------------------------------------------------------


class TestGateBearingRegistrationFailureRefusesFire:
    def test_register_obligation_raising_refuses_the_fire_no_worker_invoked(
        self, tmp_path, monkeypatch, capsys,
    ):
        data_dir, spec_file = _make_bundle(
            tmp_path,
            staging_id="20260906-staging-gate-fail",
            dispatch_id="20260906-a3-gate-fail",
            gate="codex_gate",
            dispatch_paths=["scripts/lib/dispatch_cli.py"],
            role="backend-developer",
        )
        _make_state_dir(tmp_path)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        def _raising_register_obligation(*_args, **_kwargs):
            raise OSError("simulated: obligation write failed")

        monkeypatch.setattr(gate_obligations, "register_obligation", _raising_register_obligation)

        with patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_headless:
            rc = run_dispatch(spec_file)

        assert rc == 1, "a gate-bearing dispatch whose obligation cannot be registered must be refused"
        mock_headless.assert_not_called()

        err = capsys.readouterr().err
        assert "gate-obligation-registration-failed" in err
        assert "obligation write failed" in err

    def test_verdict_is_returned_directly_from_register_gate_obligation(self, tmp_path, monkeypatch):
        """Narrower unit-level pin on _register_gate_obligation itself, independent
        of the full run_dispatch plumbing above."""
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)

        def _raising_register_obligation(*_args, **_kwargs):
            raise OSError("simulated: obligation write failed")

        monkeypatch.setattr(gate_obligations, "register_obligation", _raising_register_obligation)

        import types
        spec = types.SimpleNamespace(
            gate="codex_gate", dispatch_id="20260906-a3-unit-gate-fail",
            project_id="vnx-dev", pr_id=None,
        )

        verdict = dispatch_cli._register_gate_obligation(spec, state_dir=state_dir)

        assert verdict is not None
        assert verdict.code == "gate-obligation-registration-failed"
        assert verdict.severity == "blocking"
        assert "obligation write failed" in verdict.message

        # The failure is still recorded as a fact (both ledgers), never silent.
        facts = [
            r for r in _read_ndjson(state_dir / "dispatch_register.ndjson")
            if r.get("event") == "door_bookkeeping_failed"
        ]
        assert facts, "expected a door_bookkeeping_failed record in dispatch_register.ndjson"
        assert facts[-1]["extra"]["site"] == "_register_gate_obligation"


# ---------------------------------------------------------------------------
# Part 2: the no-gate route keeps its original best-effort contract.
# ---------------------------------------------------------------------------


class TestNoGateRouteStaysBestEffort:
    def test_register_no_gate_obligation_raising_does_not_block_a_read_only_fire(
        self, tmp_path, monkeypatch,
    ):
        data_dir, spec_file = _make_bundle(
            tmp_path,
            staging_id="20260906-staging-nogate-fail",
            dispatch_id="20260906-a3-nogate-fail",
            gate="",
            dispatch_paths=[],
            role="code-reviewer",
        )
        _make_state_dir(tmp_path)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        import smart_router

        def _router_boom(**_kwargs):
            raise RuntimeError("smart-router derivation exploded")

        monkeypatch.setattr(smart_router, "resolve_gate", _router_boom)

        def _raising_register_no_gate_obligation(*_args, **_kwargs):
            raise OSError("simulated: no-gate record write failed")

        monkeypatch.setattr(
            gate_obligations, "register_no_gate_obligation", _raising_register_no_gate_obligation,
        )

        with patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_headless:
            rc = run_dispatch(spec_file)

        assert rc == 0, "a read-only dispatch must still fire even when its no-gate record fails"
        mock_headless.assert_called_once()

        facts = [
            r for r in _read_ndjson(data_dir / "state" / "dispatch_register.ndjson")
            if r.get("event") == "door_bookkeeping_failed"
        ]
        assert facts, "expected a door_bookkeeping_failed record for the failed no-gate write"
        assert facts[-1]["dispatch_id"] == "20260906-a3-nogate-fail"
        assert facts[-1]["extra"]["site"] == "_register_gate_obligation"
        assert "no-gate record write failed" in facts[-1]["extra"]["error"]

        receipt_facts = [
            r for r in _read_ndjson(data_dir / "state" / "t0_receipts.ndjson")
            if r.get("event_type") == "door_bookkeeping_failed"
        ]
        assert receipt_facts, "expected a door_bookkeeping_failed receipt in t0_receipts.ndjson"
        assert receipt_facts[-1]["dispatch_id"] == "20260906-a3-nogate-fail"
        assert receipt_facts[-1]["site"] == "_register_gate_obligation"


# ---------------------------------------------------------------------------
# Part 3: _record_bookkeeping_failure reaches stderr (directly) and the
# receipt ledger, in addition to the pre-existing dispatch_register.ndjson write.
# ---------------------------------------------------------------------------


class TestRecordBookkeepingFailureReachesStderrAndReceiptLedger:
    def test_stderr_and_receipt_ledger_both_carry_the_fact(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)

        dispatch_cli._record_bookkeeping_failure(
            "unit-test-site", "20260906-a3-direct-unit", ValueError("boom"), state_dir=state_dir,
        )

        err = capsys.readouterr().err
        assert "door bookkeeping failed site=unit-test-site" in err
        assert "20260906-a3-direct-unit" in err
        assert "boom" in err

        receipt_facts = [
            r for r in _read_ndjson(state_dir / "t0_receipts.ndjson")
            if r.get("event_type") == "door_bookkeeping_failed"
        ]
        assert receipt_facts, "expected a door_bookkeeping_failed receipt in t0_receipts.ndjson"
        fact = receipt_facts[-1]
        assert fact["dispatch_id"] == "20260906-a3-direct-unit"
        assert fact["site"] == "unit-test-site"
        assert "boom" in fact["error"]

        # The pre-existing dispatch_register.ndjson write must still happen —
        # this fix is additive, not a replacement.
        register_facts = [
            r for r in _read_ndjson(state_dir / "dispatch_register.ndjson")
            if r.get("event") == "door_bookkeeping_failed"
        ]
        assert register_facts, "the original dispatch_register.ndjson fact must still be written"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
