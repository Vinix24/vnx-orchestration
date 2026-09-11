"""A decided gate verdict books a RESULT receipt with verdict + provider + model (OI-1702/OI-1703).

The request-time writers (gate_request_handler) book a ``review_gate`` receipt
when the gate is REQUESTED, so the ledger's 775 gate receipts all describe
requests (investigate 753, reject 22, accept 0). The result writers
(``write_result_guarded`` and ``record_terminal_result``) never booked a
second receipt for the OUTCOME, so 456 decided results (237 completed + 219
pass) were invisible to the ledger, and the provider/model the result files
already carried never reached it either.

This file pins the fix on both write paths:

1. a completed gate yields a receipt whose ``verdict.decision`` is ``accept``
   (RED on main: no result receipt existed at all);
2. ``provider``/``model`` land on that receipt from the payload the writer
   already holds, and are omitted rather than derived from the gate name;
3. a REFUSED write yields no result receipt (the ``written`` gate);
4. a ``completed`` record WITH blocking findings books ``reject``, never
   ``accept`` -- decidedness is ``gate_status.is_pass``, not the raw status
   literal, so a rejected PR is never minted a clean accept.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_depth
from gate_recorder import record_terminal_result, write_result_guarded

# Non-degenerate single-shot depth: just enough that record_terminal_result
# never trips the (unrelated) gate_execution_degenerate reclassification.
_OK_DEPTH = gate_depth.single_shot_depth(50000, True)


def _receipts(state_dir: Path) -> list[dict]:
    ledger = state_dir / "t0_receipts.ndjson"
    if not ledger.exists():
        return []
    return [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _result_receipts(state_dir: Path) -> list[dict]:
    return [r for r in _receipts(state_dir) if r.get("event_type") == "review_gate_result"]


def _pass_payload() -> dict:
    return {
        "gate": "glm_gate",
        "pr_id": "99",
        "status": "pass",
        "contract_hash": "realhash",
        "report_path": "/tmp/glm-report-99.md",
        "dispatch_id": "glm-gate-pr99-1",
        "blocking_findings": [],
        "provider": "glm-harness",
        "model": "glm-5.2",
    }


# ---------------------------------------------------------------------------
# 1. The accept receipt -- RED on main: a completed gate yielded no result
#    receipt, so the ledger had zero accepts from 456 decided outcomes.
# ---------------------------------------------------------------------------


def test_completed_gate_emits_accept_verdict_receipt(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

    record_terminal_result(
        gate="glm_gate", pr_id="99", result_path=tmp_path / "pr-99-glm_gate.json",
        payload=_pass_payload(), execution_depth=_OK_DEPTH,
    )

    receipts = _result_receipts(state_dir)
    assert len(receipts) == 1, (
        "a decided pass must book exactly one review_gate_result receipt; "
        f"got {receipts!r}"
    )
    receipt = receipts[0]
    assert receipt["receipt_kind"] == "review_gate"
    assert receipt["status"] == "completed"
    assert receipt["verdict"]["decision"] == "accept"


# ---------------------------------------------------------------------------
# 2. Provider + model come from the payload the writer already holds, not
#    from the gate name.
# ---------------------------------------------------------------------------


def test_result_receipt_carries_provider_and_model_from_payload(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

    record_terminal_result(
        gate="glm_gate", pr_id="99", result_path=tmp_path / "pr-99-glm_gate.json",
        payload=_pass_payload(), execution_depth=_OK_DEPTH,
    )

    receipt = _result_receipts(state_dir)[0]
    assert receipt["provider"] == "glm-harness"
    assert receipt["model"] == "glm-5.2"


def test_result_receipt_omits_provider_when_payload_has_none(tmp_path, monkeypatch):
    """The gate name is never a provider fallback: a payload without
    ``provider`` yields a receipt without ``provider``, even though the gate
    field names the lane that produced it."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

    payload = _pass_payload()
    payload["gate"] = "kimi_gate"
    payload.pop("provider")
    payload.pop("model")
    record_terminal_result(
        gate="kimi_gate", pr_id="99", result_path=tmp_path / "pr-99-kimi_gate.json",
        payload=payload, execution_depth=_OK_DEPTH,
    )

    receipt = _result_receipts(state_dir)[0]
    assert receipt["gate"] == "kimi_gate"
    assert "provider" not in receipt
    assert "model" not in receipt


# ---------------------------------------------------------------------------
# 3. Only a write that landed books a receipt. A refused write left another
#    writer's record standing, so there is no outcome of THIS write to book.
# ---------------------------------------------------------------------------


def test_refused_write_emits_no_result_receipt(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

    out = tmp_path / "pr-42-codex_gate.json"
    existing = {
        "gate": "codex_gate", "pr_id": "42", "status": "completed",
        "contract_hash": "decidedhash", "report_path": "/tmp/codex-report-42.md",
        "dispatch_id": "codex-gate-pr42-1", "blocking_findings": [],
    }
    out.write_text(json.dumps(existing), encoding="utf-8")

    # An evidence-less ``failed`` write over a decided, evidenced verdict: the
    # guard refuses it (downgrade), and it would have booked a reject receipt
    # had it landed -- so this tests the ``written`` gate, not a status that
    # is simply outside PASS/FAIL.
    attempted = {
        "gate": "codex_gate", "pr_id": "42", "status": "failed",
        "contract_hash": "", "report_path": "",
        "dispatch_id": "codex-gate-pr42-2", "blocking_findings": [],
    }
    _payload, written = write_result_guarded(out, attempted, gate="codex_gate", pr_ref="42")

    assert written is False
    assert _result_receipts(state_dir) == []


# ---------------------------------------------------------------------------
# 4. Decidedness is is_pass, not the raw status literal. A ``completed``
#    record with blocking findings is a FAIL -- booking it as accept would
#    mint a clean verdict for a rejected PR.
# ---------------------------------------------------------------------------


def test_completed_with_blocking_findings_books_reject_not_accept(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

    payload = {
        "gate": "codex_gate", "pr_id": "101", "status": "completed",
        "contract_hash": "realhash", "report_path": "/tmp/codex-report-101.md",
        "dispatch_id": "codex-gate-pr101-1",
        "blocking_findings": [{"severity": "blocking", "title": "broken invariant"}],
        "provider": "codex",
        "model": "codex-gpt-5.2",
    }
    _payload, written = write_result_guarded(
        tmp_path / "pr-101-codex_gate.json", payload, gate="codex_gate", pr_ref="101",
    )

    assert written is True
    receipt = _result_receipts(state_dir)[0]
    assert receipt["status"] == "failed"
    assert receipt["blocking_count"] == 1
    assert receipt["verdict"]["decision"] == "reject"


# ---------------------------------------------------------------------------
# 5. The result-receipt write path must not fork a subprocess.
# ---------------------------------------------------------------------------


def test_result_receipt_write_path_spawns_no_subprocess(tmp_path, monkeypatch):
    """Booking the outcome receipt must not spawn a subprocess.

    OI-1702/OI-1703 regression (dispatch
    20260911-fix1836-subprocess-uit-schrijfpad): the receipt write used to
    resolve its store via facade.ensure_env() -> resolve_paths() ->
    _git_toplevel(), which forks ``git rev-parse --show-toplevel`` on every
    append. That put a subprocess in the write path of every gate outcome and
    broke tests that patch subprocess.Popen to mock the codex run
    (tests/test_gate_runner.py). A decided gate result is already on disk by
    the time its receipt is booked; the writer must not fork.
    """
    state_dir = tmp_path / "state"
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

    with mock.patch("subprocess.Popen") as popen:
        record_terminal_result(
            gate="glm_gate", pr_id="99", result_path=tmp_path / "pr-99-glm_gate.json",
            payload=_pass_payload(), execution_depth=_OK_DEPTH,
        )

    popen.assert_not_called()
    assert len(_result_receipts(state_dir)) == 1, (
        "the outcome receipt must still be written"
    )
