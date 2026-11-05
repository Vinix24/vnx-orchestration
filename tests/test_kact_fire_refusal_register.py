"""OI-2536: a refused refire next to a live attempt is no dispatch outcome.

The door books a refused ``refire_attempt_live`` fire as one
``blocked``/``delivery_refused`` receipt (ADR-005, K-4c). That receipt is scoped
to THIS fire: another attempt of the same dispatch is still live, so the refusal
says nothing about the dispatch's outcome. Before this fix the register emitter
read ``blocked`` as a governed failure and wrote ``dispatch_failed`` for the
dispatch while its live attempt kept running, and
``t0_decision_reconcile`` resolved the pending decision as failed at the first
observation.

Every store is a real one under ``tmp_path``: the door writes the receipt
through ``governance_emit.emit_dispatch_receipt``, and the register file is
written by the REAL ``_emit_dispatch_register`` / ``dispatch_register`` writer
(no stub of either). ``_emit_dispatch_register`` is invoked on the receipt the
door actually wrote — the same dict the append layer hands it.

Red on ``be18cdcc`` (origin/main): the first test finds ``dispatch_failed`` in
the register for the still-live dispatch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "tests"))

import dispatch_register  # noqa: E402
import t0_decision_reconcile  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from append_receipt_internals.register_emit import _emit_dispatch_register  # noqa: E402
from test_k4b_door_hand_over import (  # noqa: E402
    _door_env,
    _make_bundle,
    _seed_live_attempt,
)
from test_k4c_start_refusal_receipt import (  # noqa: E402
    _drive_row_past_the_queue,
    _refusals,
    _run_door,
    _seed_start_line,
)

# The ledger contract value the door stamps on a fire-scoped refusal receipt.
FIRE_SCOPE = "fire"


@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch, tmp_path):
    """Keep the append layer real, but never fire its process-spawning hooks.

    Unlike the K-4b/K-4c fixture this one does NOT stub
    ``_emit_dispatch_register``: the register writer under test is the real one.
    Only its central mirror is redirected so no test run can reach the real
    central store.
    """
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr(
        dispatch_register, "_resolve_central_data_dir",
        lambda project_id: tmp_path / "central",
    )


def _register_events(state_dir: Path) -> list:
    path = state_dir / "dispatch_register.ndjson"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _events_for(state_dir: Path, dispatch_id: str) -> list:
    return [event for event in _register_events(state_dir)
            if event.get("dispatch_id") == dispatch_id]


def _pin_state(monkeypatch, state_dir: Path) -> None:
    """Point the ambient resolver at the store the door writes to.

    conftest's isolation fixture pins VNX_STATE_DIR to its own tmp tree; the
    register writer is the one caller that resolves its path ambiently, so the
    pin has to name the same state dir the door was given.
    """
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))


def _seed_register_start(state_dir: Path, dispatch_id: str) -> None:
    """The register already holds the live attempt's ``dispatch_started``."""
    assert dispatch_register.append_event(
        "dispatch_started", dispatch_id=dispatch_id, state_dir=state_dir) is True


def _drive_refire_refusal(tmp_path: Path, monkeypatch, dispatch_id: str):
    """Run the real door into a real ``refire_attempt_live`` refusal."""
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id, base_ref="HEAD")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)
    _pin_state(monkeypatch, state_dir)
    _seed_live_attempt(state_dir, dispatch_id=dispatch_id, attempt_number=1)
    _drive_row_past_the_queue(state_dir, dispatch_id)
    _seed_start_line(state_dir, dispatch_id, attempt=1)
    rc_value, headless = _run_door(spec_file, state_dir, {})
    return state_dir, rc_value, headless


def test_a_live_refire_refusal_never_becomes_dispatch_failed(tmp_path, monkeypatch, capsys):
    dispatch_id = "20261105-oi2536-refire-live"
    state_dir, rc_value, headless = _drive_refire_refusal(tmp_path, monkeypatch, dispatch_id)
    assert rc_value == 1
    headless.assert_not_called()
    assert "REJECT [start-line-refused]: refire_attempt_live" in capsys.readouterr().err

    _seed_register_start(state_dir, dispatch_id)
    [refusal] = _refusals(state_dir, dispatch_id)

    emitted = _emit_dispatch_register(refusal)

    events = [event["event"] for event in _events_for(state_dir, dispatch_id)]
    assert "dispatch_started" in events, events
    assert "dispatch_failed" not in events, events
    assert emitted is False


def test_the_fire_scoped_refusal_receipt_stays(tmp_path, monkeypatch, capsys):
    dispatch_id = "20261105-oi2536-receipt-stays"
    state_dir, rc_value, headless = _drive_refire_refusal(tmp_path, monkeypatch, dispatch_id)
    assert rc_value == 1
    headless.assert_not_called()
    capsys.readouterr()

    refusals = _refusals(state_dir, dispatch_id)
    assert len(refusals) == 1, refusals
    [receipt] = refusals
    assert receipt["status"] == "blocked"
    assert receipt["delivery_state"] == "delivery_refused"
    assert receipt.get("refusal_scope") == FIRE_SCOPE


def test_another_start_refusal_still_produces_dispatch_failed(tmp_path, monkeypatch, capsys):
    dispatch_id = "20261105-oi2536-base-refused"
    data_dir, spec_file = _make_bundle(
        tmp_path, dispatch_id=dispatch_id, base_ref="not-a-ref-anywhere")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)
    _pin_state(monkeypatch, state_dir)

    rc_value, headless = _run_door(spec_file, state_dir, {})
    assert rc_value == 1
    headless.assert_not_called()
    assert "REJECT [start-line-refused]: base_ref_unresolved" in capsys.readouterr().err

    [refusal] = _refusals(state_dir, dispatch_id)
    assert "refusal_scope" not in refusal

    assert _emit_dispatch_register(refusal) is True
    events = [event["event"] for event in _events_for(state_dir, dispatch_id)]
    assert "dispatch_failed" in events, events


def test_reconcile_does_not_resolve_the_live_dispatch_as_failed(tmp_path, monkeypatch, capsys):
    dispatch_id = "20261105-oi2536-reconcile"
    state_dir, rc_value, headless = _drive_refire_refusal(tmp_path, monkeypatch, dispatch_id)
    assert rc_value == 1
    headless.assert_not_called()
    capsys.readouterr()

    _seed_register_start(state_dir, dispatch_id)
    [refusal] = _refusals(state_dir, dispatch_id)
    _emit_dispatch_register(refusal)

    decision_log = state_dir / "t0_decision_log.jsonl"
    decision_log.write_text(json.dumps({
        "timestamp": "2026-11-05T00:00:00Z",
        "decision_type": "dispatch_created",
        "dispatch_id": dispatch_id,
        "outcome_pending": True,
        "expected_outcome": "success",
    }) + "\n", encoding="utf-8")
    outcomes_log = state_dir / "t0_decision_outcomes.ndjson"

    written = t0_decision_reconcile.reconcile(
        decision_log=decision_log,
        register_path=state_dir / "dispatch_register.ndjson",
        outcomes_log=outcomes_log,
        cursor_file=state_dir / "t0_decision_outcomes_cursor.json",
    )

    assert written == 0
    assert not outcomes_log.exists() or outcomes_log.read_text(encoding="utf-8").strip() == ""
