"""ADR-045 audit-spoor-kern K-4c round 2: a refused start leaves a receipt.

Dispatch-ID: dlv-2326a0e02534-r2

ADR-005 keeps every door decision in the NDJSON ledger. K-4b printed
``REJECT [start-line-refused]`` and closed the attempt the door owned, but booked
nothing when it owned none (a refire while the previous attempt is still live) and
left only DB state even when it did. Both refusals now go through the same writer
the provider-switch refusal uses: one ``blocked``/``delivery_refused`` receipt,
nothing spent, whose failure_reason names ``start-line-refused`` and the code.

Every store under ``tmp_path``; the helpers come from the K-4b suite, not a copy.

Red on ``05684b05``: a refused start wrote no receipt at all.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_cli  # noqa: E402
import governance_emit  # noqa: E402
import runtime_coordination as rc  # noqa: E402
from failure_classification import FAILURE_CLASSES  # noqa: E402
from receipt_schema import DISPATCH_FINISHED_EVENT_TYPE, DISPATCH_STARTED_EVENT_TYPE  # noqa: E402
from test_k4b_door_hand_over import (  # noqa: E402
    PROJECT, _attempt_rows, _clean_snapshot, _dispatch_row, _hermetic_append,
    _make_bundle, _door_env, _seed_live_attempt, _slot_spy, _start_lines,
)

PROVIDER = "claude"


@pytest.fixture(autouse=True)
def _hermetic(_hermetic_append):
    """K-4b's fixture, imported: the append layer stays real, its process hooks never fire."""
    return _hermetic_append


def _receipts(state_dir: Path) -> list:
    ledger = state_dir / "t0_receipts.ndjson"
    if not ledger.is_file():
        return []
    out = []
    for raw in ledger.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("receipt_kind") == "dispatch":
            out.append(rec)
    return out


def _refusals(state_dir: Path, dispatch_id: str) -> list:
    return [r for r in _receipts(state_dir)
            if r.get("dispatch_id") == dispatch_id
            and r.get("delivery_state") == "delivery_refused"]


def _end_lines(state_dir: Path, dispatch_id: str) -> list:
    ledger = state_dir / "t0_receipts.ndjson"
    if not ledger.is_file():
        return []
    out = []
    for raw in ledger.read_text(encoding="utf-8").splitlines():
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (isinstance(line, dict) and line.get("dispatch_id") == dispatch_id
                and line.get("event_type") == DISPATCH_FINISHED_EVENT_TYPE):
            out.append(line)
    return out


def _seed_start_line(state_dir: Path, dispatch_id: str, *, attempt: int) -> None:
    """A start line whose door pid is this live process — the previous attempt is real."""
    line = {
        "timestamp": "2026-11-03T00:00:00Z",
        "event_type": DISPATCH_STARTED_EVENT_TYPE,
        "project_id": PROJECT,
        "dispatch_id": dispatch_id,
        "attempt": attempt,
        "role": "backend-developer",
        "base_sha": "0" * 40,
        "spec_sha256": "0" * 64,
        "lifecycle_mode": "v3",
        "door_pid": os.getpid(),
        "door_started_at": dispatch_cli.dispatch_start.process_started_at(os.getpid()),
    }
    ledger = state_dir / "t0_receipts.ndjson"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _drive_row_past_the_queue(state_dir: Path, dispatch_id: str) -> None:
    """Leave the row where _persist_dispatch_row cannot claim it (a refire of a live one)."""
    with rc.get_connection(state_dir) as conn:
        for state in ("claimed", "delivering"):
            rc.transition_dispatch(conn, dispatch_id=dispatch_id, to_state=state, actor="test")
        conn.commit()


def _run_door(spec_file: Path, state_dir: Path, seen: dict, *, refire_reason=None):
    with patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli.serialize_lane", side_effect=lambda *a, **k: _slot_spy(state_dir, seen)), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as headless:
        return dispatch_cli.run_dispatch(spec_file, refire_reason=refire_reason), headless


def _run_provider_door(spec_file: Path, state_dir: Path, seen: dict, *, refire_reason=None):
    """The same real door on the provider lane: the lane executor is a spy."""
    envelope = MagicMock(return_value=MagicMock(returncode=0, status="success", error=None))
    with patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli.serialize_lane", side_effect=lambda *a, **k: _slot_spy(state_dir, seen)), \
         patch("dispatch_cli._check_reachability",
               return_value=dispatch_cli.ReachabilityVerdict(block=False)), \
         patch("dispatch_cli.run_envelope_plan", envelope):
        return dispatch_cli.run_dispatch(spec_file, refire_reason=refire_reason), envelope


def _switch_file(tmp_path: Path, *, deepseek_enabled: bool) -> Path:
    """The operator's switch file, driven through VNX_PROVIDER_SWITCH_FILE."""
    path = tmp_path / "switch" / "providers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1,
        "global": {"deepseek": {
            "enabled": deepseek_enabled, "changed_at": "2026-11-03T00:00:00Z",
            "changed_by": "test", "reason": "K-4c round 3",
        }},
        "projects": {},
    }), encoding="utf-8")
    return path


_COMPLETION_BODY = (
    "## Summary\n\nA complete implementation with enough words to satisfy the body "
    "contract of every report the fabric reads, written after a refused fire.\n\n"
    "## Changes\n\n- `scripts/lib/envelope_govern_support.py`\n\n"
    "## Verification\n\n- pytest tests/test_k4c_start_refusal_receipt.py -q\n\n"
    "## Open Items\n\nNone\n"
)


def _govern_the_refire(
    data_dir: Path, state_dir: Path, dispatch_id: str, *, provider: str = PROVIDER,
) -> None:
    """Run the lane's real end-of-dispatch GOVERN against the SAME ledger the refusal hit.

    The only question this test asks of it: does GOVERN see the earlier refusal as a lane
    completion (and skip its own receipt) or not?
    """
    from datetime import datetime

    import envelope_govern
    from envelope_types import EnvelopeSpec, _AdapterResult

    report = data_dir / "unified_reports" / f"{dispatch_id}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(_COMPLETION_BODY, encoding="utf-8")
    spec = EnvelopeSpec(
        dispatch_id=dispatch_id, terminal_id="T0", provider=provider, model="sonnet",
        instruction="do the thing", role="backend-developer", pr_id=None,
        state_dir=state_dir, data_dir=data_dir,
    )
    result = _AdapterResult(returncode=0, completion_text=_COMPLETION_BODY, status="success")
    with patch("envelope_govern._archive_dispatch_events", return_value=("", True)), \
         patch("envelope_govern._clear_dispatch_events"):
        envelope_govern._govern(
            spec, result,
            start_time=datetime(2026, 11, 3, 12, 0, 0),
            end_time=datetime(2026, 11, 3, 12, 1, 0),
        )


def _completions(state_dir: Path, dispatch_id: str) -> list:
    return [r for r in _receipts(state_dir)
            if r.get("dispatch_id") == dispatch_id
            and r.get("delivery_state") != "delivery_refused"]


# ---------------------------------------------------------------------------
# 1. no owned attempt (a live refire): one receipt, the live attempt untouched
# ---------------------------------------------------------------------------

def test_a_live_refire_is_refused_and_still_booked(tmp_path, monkeypatch, capsys):
    dispatch_id = "20261103-k4c-refire-live"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id, base_ref="HEAD")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)
    live_attempt_id = _seed_live_attempt(state_dir, dispatch_id=dispatch_id, attempt_number=1)
    _drive_row_past_the_queue(state_dir, dispatch_id)
    _seed_start_line(state_dir, dispatch_id, attempt=1)

    row_before = _dispatch_row(state_dir, dispatch_id)
    attempts_before = _attempt_rows(state_dir, dispatch_id)
    starts_before = _start_lines(state_dir)

    seen: dict = {}
    rc_value, headless = _run_door(spec_file, state_dir, seen)

    assert rc_value == 1
    assert "start_lines_at_slot" not in seen, "a refused start must not take the slot"
    headless.assert_not_called()
    assert "REJECT [start-line-refused]: refire_attempt_live" in capsys.readouterr().err

    # (a) exactly ONE receipt for this refusal, owned attempt or not
    refusals = _refusals(state_dir, dispatch_id)
    assert len(refusals) == 1, refusals
    [receipt] = refusals
    assert receipt["status"] == "blocked" and receipt["receipt_kind"] == "dispatch"
    assert receipt["provider"] == PROVIDER and receipt["model"]
    assert "start-line-refused" in receipt["failure_reason"]
    assert "refire_attempt_live" in receipt["failure_reason"]
    assert receipt["token_usage"]["input"] == 0 and receipt["token_usage"]["output"] == 0
    assert receipt["cost_usd"] == 0.0
    assert receipt["failure_class"] in FAILURE_CLASSES
    assert receipt["failure_class"] not in {"auth_rejected", "credit_exhausted"}

    # (b) the other door's live attempt is untouched — nothing was closed for it.
    # K-6b: the door's bounded sweep runs before this refusal and stamps the live
    # v3 attempt's row with ``lifecycle_mode`` (ADR-045 §6); no other field moves.
    row_after = _dispatch_row(state_dir, dispatch_id)
    assert json.loads(row_after["metadata_json"]).get("lifecycle_mode") == "v3"
    volatile = ("metadata_json", "updated_at")
    assert {k: v for k, v in row_after.items() if k not in volatile} == {
        k: v for k, v in row_before.items() if k not in volatile}
    assert _attempt_rows(state_dir, dispatch_id) == attempts_before
    assert list(_attempt_rows(state_dir, dispatch_id)) == [1], "no attempt was created"
    assert _attempt_rows(state_dir, dispatch_id)[1]["attempt_id"] == live_attempt_id
    assert _attempt_rows(state_dir, dispatch_id)[1]["state"] == "running"
    assert _start_lines(state_dir) == starts_before, "no start line was added"
    assert _end_lines(state_dir, dispatch_id) == [], "no dispatch_finished for the live attempt"


# ---------------------------------------------------------------------------
# 2. an owned attempt is closed as before — and the refusal is booked too
# ---------------------------------------------------------------------------

def test_an_owned_attempt_refusal_is_booked_and_still_closes_the_attempt(tmp_path, monkeypatch, capsys):
    dispatch_id = "20261103-k4c-owned-refused"
    data_dir, spec_file = _make_bundle(
        tmp_path, dispatch_id=dispatch_id, base_ref="not-a-ref-anywhere")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)

    seen: dict = {}
    rc_value, headless = _run_door(spec_file, state_dir, seen)

    assert rc_value == 1
    headless.assert_not_called()
    assert "REJECT [start-line-refused]: base_ref_unresolved" in capsys.readouterr().err

    refusals = _refusals(state_dir, dispatch_id)
    assert len(refusals) == 1, refusals
    assert "start-line-refused" in refusals[0]["failure_reason"]
    assert "base_ref_unresolved" in refusals[0]["failure_reason"]

    attempts = _attempt_rows(state_dir, dispatch_id)
    assert list(attempts) == [1]
    assert attempts[1]["state"] == "failed"
    assert attempts[1]["failure_reason"] == "base_ref_unresolved"
    assert _dispatch_row(state_dir, dispatch_id)["state"] == "failed_delivery"


# ---------------------------------------------------------------------------
# 3. the refusal never poisons the lane or the dispatch
# ---------------------------------------------------------------------------

def test_a_refusal_receipt_neither_cools_the_lane_nor_blocks_the_refire(tmp_path, monkeypatch, capsys):
    dispatch_id = "20261103-k4c-clean-refire"
    data_dir, spec_file = _make_bundle(
        tmp_path, dispatch_id=dispatch_id, base_ref="not-a-ref-anywhere")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)

    rc_value, headless = _run_door(spec_file, state_dir, {})
    assert rc_value == 1
    headless.assert_not_called()
    capsys.readouterr()
    refusal = _refusals(state_dir, dispatch_id)
    assert len(refusal) == 1, refusal

    # The refusal as it lands on the ledger, alone: no route decision, no runtime row.
    ledger_only = tmp_path / "ledger-only"
    ledger_only.mkdir()
    (ledger_only / "t0_receipts.ndjson").write_text(
        json.dumps(refusal[0], separators=(",", ":")) + "\n", encoding="utf-8")

    assert dispatch_cli._recent_blocking_rejection(PROVIDER, ledger_only) is None
    assert dispatch_cli._refire_guard_verdict(
        dispatch_id, state_dir=ledger_only, refire_reason=None) is None

    # Control: a REAL failed receipt for the same dispatch still blocks the refire —
    # the pass-over is the refusal's, not a blanket "blocked is not terminal".
    real_failure = dict(refusal[0], status="failed", failure_class="unknown",
                        failure_reason="provider=claude returncode=1: the lane died")
    (ledger_only / "t0_receipts.ndjson").write_text(
        json.dumps(refusal[0], separators=(",", ":")) + "\n"
        + json.dumps(real_failure, separators=(",", ":")) + "\n", encoding="utf-8")
    assert dispatch_cli._refire_guard_verdict(
        dispatch_id, state_dir=ledger_only, refire_reason=None) is not None


# ---------------------------------------------------------------------------
# 4. a receipt that cannot be written still refuses the fire, loudly booked
# ---------------------------------------------------------------------------

def test_a_refusal_receipt_that_cannot_be_written_still_refuses_the_fire(
        tmp_path, monkeypatch, capsys):
    dispatch_id = "20261103-k4c-receipt-broken"
    data_dir, spec_file = _make_bundle(
        tmp_path, dispatch_id=dispatch_id, base_ref="not-a-ref-anywhere")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)

    def broken(**kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(governance_emit, "emit_dispatch_receipt", broken)

    rc_value, headless = _run_door(spec_file, state_dir, {})
    assert rc_value == 1
    headless.assert_not_called()
    err = capsys.readouterr().err
    assert "REJECT [start-line-refused]: base_ref_unresolved" in err
    assert "door bookkeeping failed site=start_line_refusal_receipt" in err
    assert _refusals(state_dir, dispatch_id) == []


# ---------------------------------------------------------------------------
# 5. ledger_lifecycle off, no refusal: the fire is 05684b05's fire
# ---------------------------------------------------------------------------

def test_legacy_fire_without_a_refusal_writes_no_extra_receipt(tmp_path, monkeypatch):
    dispatch_id = "20261103-k4c-legacy-clean"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id)
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=False)

    seen: dict = {}
    rc_value, headless = _run_door(spec_file, state_dir, seen)

    assert rc_value == 0
    headless.assert_called_once()
    assert _refusals(state_dir, dispatch_id) == []
    assert [r for r in _receipts(state_dir) if r.get("delivery_state")] == []
    assert _start_lines(state_dir) == []
    decision = json.loads(
        (state_dir / "route_decisions" / f"{dispatch_id}.json").read_text(encoding="utf-8"))
    assert decision["decision"]["lane"] == "claude_headless"


# ---------------------------------------------------------------------------
# 6. a refusal is never the lane's completion: the refire's GOVERN still books
# ---------------------------------------------------------------------------

def test_a_refused_start_then_a_fixed_base_refire_keeps_both_lines(
        tmp_path, monkeypatch, capsys):
    """ADR-045 K-4c round 3: the refusal must not stand in for the refire's completion."""
    dispatch_id = "20261103-k4c-base-refused-then-fixed"
    data_dir, spec_file = _make_bundle(
        tmp_path, dispatch_id=dispatch_id, base_ref="not-a-ref-anywhere")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)

    rc_value, headless = _run_door(spec_file, state_dir, {})
    assert rc_value == 1
    headless.assert_not_called()
    assert "REJECT [start-line-refused]: base_ref_unresolved" in capsys.readouterr().err
    assert len(_refusals(state_dir, dispatch_id)) == 1

    # The base is fixed and the SAME dispatch fires again — and succeeds.
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    spec["base_ref"] = "origin/main"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")

    rc_value, headless = _run_door(
        spec_file, state_dir, {}, refire_reason="base ref fixed upstream")
    assert rc_value == 0
    headless.assert_called_once()

    # The lane's own GOVERN step: it must not mistake the refusal for its completion.
    _govern_the_refire(data_dir, state_dir, dispatch_id)

    refusals = _refusals(state_dir, dispatch_id)
    completions = _completions(state_dir, dispatch_id)
    assert len(refusals) == 1, refusals
    assert "start-line-refused" in refusals[0]["failure_reason"]
    assert len(completions) == 1, completions
    assert completions[0]["status"] == "success"
    assert completions[0]["completion_pct"] == 100


def test_a_switched_off_family_then_switched_back_on_keeps_both_lines(
        tmp_path, monkeypatch, capsys):
    """The same hole on the other door refusal: provider switch off, then back on."""
    dispatch_id = "20261103-k4c-switch-refused-then-on"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id)
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    spec["provider"] = "deepseek-harness"
    spec["model"] = "deepseek-v4-pro"
    spec["target_slot"] = "T1"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("VNX_PROVIDER_SWITCH_FILE", str(_switch_file(tmp_path, deepseek_enabled=False)))

    rc_value, envelope = _run_provider_door(spec_file, state_dir, {})
    assert rc_value == 1
    envelope.assert_not_called()
    assert "REJECT [provider-disabled]" in capsys.readouterr().err
    [refusal] = _refusals(state_dir, dispatch_id)
    assert refusal["failure_class"] == "provider_switched_off"

    # The operator switches the family back on; the same dispatch fires and succeeds.
    _switch_file(tmp_path, deepseek_enabled=True)
    rc_value, envelope = _run_provider_door(
        spec_file, state_dir, {}, refire_reason="provider family back on")
    assert rc_value == 0
    envelope.assert_called_once()

    _govern_the_refire(data_dir, state_dir, dispatch_id, provider="deepseek-harness")

    refusals = _refusals(state_dir, dispatch_id)
    completions = _completions(state_dir, dispatch_id)
    assert len(refusals) == 1, refusals
    assert refusals[0]["failure_class"] == "provider_switched_off"
    assert len(completions) == 1, completions
    assert completions[0]["status"] == "success"
    assert completions[0]["provider"] == "deepseek-harness"
