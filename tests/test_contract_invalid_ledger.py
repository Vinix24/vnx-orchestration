#!/usr/bin/env python3
"""Tests for contract_invalid_ledger.py and its build_t0_state.py wiring (OI-1638).

envelope_govern.py already stamps ``status: "contract_invalid"`` on a receipt
when a worker's report fails the report-body contract — that write side is
proven (350+ live receipts). Before this module nothing turned that fact
into a visible count or a non-silent-closure gate. This file covers:

  - build_contract_invalid_summary: counts + per-provider breakdown, windowed.
  - collect_contract_invalid_open / write_contract_invalid_open_ledger: the
    per-dispatch open/closed ledger (a later governed-success receipt for the
    same dispatch_id resolves it).
  - is_deliverable_acceptable: the non-silent-closure gate.
  - build_t0_state.py wiring: the ``contract_invalid`` key in the full state
    dict and its compact form in t0_index.json.

Discipline: temp-DB/temp-ledger ONLY, mirrors tests/test_human_gate_queue.py's
isolation pattern (VNX_DATA_DIR_EXPLICIT=1 + tmp VNX_DATA_DIR, central-store
fallback blocked).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"

for _p in (str(_SCRIPTS_DIR), str(_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import contract_invalid_ledger as cil  # noqa: E402
import build_t0_state as bts  # noqa: E402

_PROJECT_ID = "vnx-dev"


def _pin_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin VNX_DATA_DIR_EXPLICIT=1 + a tmp VNX_DATA_DIR; return the state dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.setattr(bts, "resolve_central_data_dir", None, raising=False)
    return state_dir


def _write_receipts(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _ci_receipt(dispatch_id: str, provider: str, timestamp: str, **overrides) -> dict:
    rec = {
        "dispatch_id": dispatch_id,
        "provider": provider,
        "status": "contract_invalid",
        "event_type": "task_complete",
        "timestamp": timestamp,
        "report_path": f"/reports/{dispatch_id}.md",
    }
    rec.update(overrides)
    return rec


def _success_receipt(dispatch_id: str, provider: str, timestamp: str, **overrides) -> dict:
    rec = {
        "dispatch_id": dispatch_id,
        "provider": provider,
        "status": "success",
        "event_type": "task_complete",
        "timestamp": timestamp,
        "report_path": f"/reports/{dispatch_id}.md",
    }
    rec.update(overrides)
    return rec


# ---------------------------------------------------------------------------
# build_contract_invalid_summary
# ---------------------------------------------------------------------------

def test_summary_counts_two_of_three_and_by_provider(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
    _write_receipts(receipts, [
        _ci_receipt("d-1", "kimi", "2026-09-06T10:00:00Z"),
        _ci_receipt("d-2", "codex", "2026-09-06T11:00:00Z"),
        _success_receipt("d-3", "claude", "2026-09-06T11:30:00Z"),
    ])

    summary = cil.build_contract_invalid_summary(receipts, now=now)

    assert summary["total"] == 2
    assert summary["by_provider"] == {"kimi": 1, "codex": 1}


def test_summary_last_24h_window_excludes_older_records(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
    _write_receipts(receipts, [
        _ci_receipt("d-old", "kimi", "2026-09-01T00:00:00Z"),  # 5 days old
        _ci_receipt("d-recent", "kimi", "2026-09-06T09:00:00Z"),  # 3 hours old
    ])

    summary = cil.build_contract_invalid_summary(receipts, now=now)

    assert summary["total"] == 2
    assert summary["last_24h"] == 1
    assert summary["by_provider_last_24h"] == {"kimi": 1}


def test_summary_empty_ledger_is_zero_no_error(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"  # never created

    summary = cil.build_contract_invalid_summary(receipts)

    assert summary["total"] == 0
    assert summary["last_24h"] == 0
    assert summary["by_provider"] == {}


def test_summary_detects_via_report_contract_invalid_event_type(tmp_path: Path) -> None:
    """Older-schema receipts (2026-06 batch) carry event_type
    'report_contract_invalid' with no distinguishing status literal issue —
    both the status literal AND the event_type marker must be recognized.
    """
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        {
            "dispatch_id": "legacy-1",
            "provider": "unknown",
            "status": "contract_invalid",
            "event_type": "report_contract_invalid",
            "timestamp": "2026-06-03T08:51:09Z",
            "report_path": "/reports/legacy-1.md",
        },
    ])

    summary = cil.build_contract_invalid_summary(receipts)
    assert summary["total"] == 1


# ---------------------------------------------------------------------------
# collect_contract_invalid_open / write_contract_invalid_open_ledger
# ---------------------------------------------------------------------------

def test_open_lists_dispatch_with_no_later_success(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-open", "kimi", "2026-09-06T10:00:00Z"),
    ])

    open_items = cil.collect_contract_invalid_open(receipts)

    assert len(open_items) == 1
    item = open_items[0]
    assert item["dispatch_id"] == "d-open"
    assert item["provider"] == "kimi"
    assert item["report_path"] == "/reports/d-open.md"
    assert item["resolved"] is False


def test_open_excludes_dispatch_healed_by_later_success(tmp_path: Path) -> None:
    """A contract_invalid attempt followed by a later success receipt for
    the SAME dispatch_id is resolved and must not appear as open."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-healed", "kimi", "2026-09-06T10:00:00Z"),
        _success_receipt("d-healed", "kimi", "2026-09-06T10:05:00Z"),
    ])

    open_items = cil.collect_contract_invalid_open(receipts)

    assert open_items == []


def test_open_reopens_if_latest_receipt_regresses_to_contract_invalid(tmp_path: Path) -> None:
    """Chronological order matters, not append order: the LATEST receipt by
    effective timestamp decides open/closed, even if it was written earlier
    in the file (out-of-order ingestion)."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _success_receipt("d-regressed", "kimi", "2026-09-06T09:00:00Z"),
        _ci_receipt("d-regressed", "kimi", "2026-09-06T10:00:00Z"),
    ])

    open_items = cil.collect_contract_invalid_open(receipts)

    assert len(open_items) == 1
    assert open_items[0]["dispatch_id"] == "d-regressed"


def test_write_ledger_is_atomic_and_readable(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-1", "kimi", "2026-09-06T10:00:00Z"),
        _ci_receipt("d-2", "codex", "2026-09-06T10:05:00Z"),
    ])
    output_path = tmp_path / "state" / cil.OPEN_LEDGER_FILENAME

    payload = cil.write_contract_invalid_open_ledger(receipts, output_path)

    assert output_path.exists()
    assert not output_path.with_suffix(output_path.suffix + ".tmp").exists()
    on_disk = json.loads(output_path.read_text(encoding="utf-8"))
    assert on_disk["open_count"] == 2
    assert payload["open_count"] == 2
    assert {i["dispatch_id"] for i in on_disk["items"]} == {"d-1", "d-2"}


# ---------------------------------------------------------------------------
# is_deliverable_acceptable
# ---------------------------------------------------------------------------

def test_is_deliverable_acceptable_false_for_open_case(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-open", "kimi", "2026-09-06T10:00:00Z"),
    ])

    ok, reason = cil.is_deliverable_acceptable("d-open", receipts)

    assert ok is False
    assert "contract_invalid" in reason


def test_is_deliverable_acceptable_true_for_healed_case(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-healed", "kimi", "2026-09-06T10:00:00Z"),
        _success_receipt("d-healed", "kimi", "2026-09-06T10:05:00Z"),
    ])

    ok, reason = cil.is_deliverable_acceptable("d-healed", receipts)

    assert ok is True
    assert "not contract_invalid" in reason


def test_is_deliverable_acceptable_true_for_absent_dispatch(tmp_path: Path) -> None:
    """No receipt at all is absence, not a rejection (OI-1624 precedent) —
    this function's contract is narrower than "did this dispatch run"."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [_ci_receipt("other-dispatch", "kimi", "2026-09-06T10:00:00Z")])

    ok, reason = cil.is_deliverable_acceptable("never-ran", receipts)

    assert ok is True
    assert "geen uitkomst-receipt" in reason
    assert "afwezigheid is geen weigering" in reason


def test_is_deliverable_acceptable_false_for_empty_dispatch_id(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    ok, reason = cil.is_deliverable_acceptable("", receipts)
    assert ok is False


# ---------------------------------------------------------------------------
# is_deliverable_acceptable: only DELIVERABLE-OUTCOME receipts are judged
# ---------------------------------------------------------------------------

def _gate_request_receipt(dispatch_id: str, timestamp: str, **overrides) -> dict:
    """A review_gate_request receipt — the gate plane, on the SAME dispatch_id.

    Shape mirrors what gate_request_handler.py emits (event_type
    review_gate_request, status requested); 844 of these sit in the live
    ledger and every one of the 8 measured masking cases had one as its last
    receipt before the merge.
    """
    rec = {
        "dispatch_id": dispatch_id,
        "provider": "claude",
        "status": "requested",
        "event_type": "review_gate_request",
        "receipt_kind": "review_gate",
        "timestamp": timestamp,
    }
    rec.update(overrides)
    return rec


def test_gate_plane_receipt_does_not_mask_contract_invalid(tmp_path: Path) -> None:
    """The measured masking case: worker outcome contract_invalid, then the
    review gate writes on the same dispatch_id. The deliverable is still
    unacceptable — a gate request says nothing about the report body."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-masked", "kimi", "2026-09-06T11:36:00Z"),
        _gate_request_receipt("d-masked", "2026-09-06T11:50:54Z"),
    ])

    ok, reason = cil.is_deliverable_acceptable("d-masked", receipts)

    assert ok is False
    assert "contract_invalid" in reason


def test_only_gate_plane_receipts_reads_as_no_outcome(tmp_path: Path) -> None:
    """Gate/state-plane receipts alone are not an outcome — absence, so no
    rejection, but the reason must say WHY it is an absence."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _gate_request_receipt("d-gate-only", "2026-09-06T11:50:54Z"),
        {"dispatch_id": "d-gate-only", "event_type": "state_mutation",
         "status": "success", "timestamp": "2026-09-06T11:55:00Z"},
    ])

    ok, reason = cil.is_deliverable_acceptable("d-gate-only", receipts)

    assert ok is True
    assert "geen uitkomst-receipt" in reason


def test_real_outcome_success_after_gate_receipt_resolves(tmp_path: Path) -> None:
    """A LATER genuine outcome still resolves the chain — the filter narrows
    which receipts count, it does not freeze the first verdict."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-really-healed", "kimi", "2026-09-06T11:36:00Z"),
        _gate_request_receipt("d-really-healed", "2026-09-06T11:50:54Z"),
        _success_receipt("d-really-healed", "kimi", "2026-09-06T12:05:00Z"),
    ])

    ok, reason = cil.is_deliverable_acceptable("d-really-healed", receipts)

    assert ok is True
    assert "not contract_invalid" in reason


def test_subprocess_completion_counts_as_outcome(tmp_path: Path) -> None:
    """subprocess_completion carries the literal 4x in the live ledger, so it
    is in DELIVERABLE_OUTCOME_EVENT_TYPES and must be judged."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-sub", "kimi", "2026-09-06T10:00:00Z",
                    event_type="subprocess_completion"),
        _gate_request_receipt("d-sub", "2026-09-06T10:30:00Z"),
    ])

    ok, _ = cil.is_deliverable_acceptable("d-sub", receipts)
    assert ok is False


def test_outcome_event_type_set_matches_measurement() -> None:
    """Pin the set: measured 07-09 over 29.386 live records, the event types
    that ever carry the contract_invalid literal are report_contract_invalid,
    task_complete and subprocess_completion; task_failed is the fourth
    deliverable outcome. review_gate_request must never be in here."""
    assert cil.DELIVERABLE_OUTCOME_EVENT_TYPES == frozenset({
        "report_contract_invalid",
        "task_complete",
        "subprocess_completion",
        "task_failed",
    })
    assert "review_gate_request" not in cil.DELIVERABLE_OUTCOME_EVENT_TYPES


# ---------------------------------------------------------------------------
# is_deliverable_acceptable: only DECIDED outcomes are chosen
#
# The plane filter above still passed 2 of the 8 measured cases: both had a
# ``task_complete``/``unknown`` written about a minute after the
# contract_invalid, and "not the contract_invalid literal" was read as
# "resolved". An undecided status decides nothing and must be skipped at
# selection time, not counted as a resolution.
# ---------------------------------------------------------------------------

def _outcome_receipt(dispatch_id: str, status: str, timestamp: str, **overrides) -> dict:
    """A deliverable-outcome receipt with an arbitrary status literal."""
    rec = {
        "dispatch_id": dispatch_id,
        "provider": "claude",
        "status": status,
        "event_type": "task_complete",
        "timestamp": timestamp,
        "ingested_at": timestamp,
    }
    rec.update(overrides)
    return rec


def test_unknown_outcome_after_contract_invalid_is_still_no_go(tmp_path: Path) -> None:
    """The measured case, field-for-field (20260830-140500-d6a2…): the
    contract_invalid receipt has NO ``ingested_at`` and the later ``unknown``
    one carries an EARLIER raw ``timestamp`` than its own ingest, so the
    effective-timestamp ordering really does put the unknown last."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d6a2", "claude", "2026-08-30T12:11:42Z", ingested_at=None),
        _outcome_receipt(
            "d6a2", "unknown", "2026-08-30T12:11:42.852911+00:00",
            ingested_at="2026-08-30T12:12:59Z",
        ),
    ])

    ok, reason = cil.is_deliverable_acceptable("d6a2", receipts)

    assert ok is False
    assert "contract_invalid" in reason
    assert "d6a2" in reason


def test_success_outcome_after_contract_invalid_is_go(tmp_path: Path) -> None:
    """The same chain with a DECIDED last outcome resolves — the filter
    narrows which receipts may be chosen, it does not freeze the verdict."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-decided", "claude", "2026-08-30T12:11:42Z", ingested_at=None),
        _outcome_receipt(
            "d-decided", "success", "2026-08-30T12:11:42.852911+00:00",
            ingested_at="2026-08-30T12:12:59Z",
        ),
    ])

    ok, reason = cil.is_deliverable_acceptable("d-decided", receipts)

    assert ok is True
    assert "not contract_invalid" in reason


def test_only_undecided_outcomes_is_go_with_geen_besliste_uitkomst(tmp_path: Path) -> None:
    """No decided outcome at all is an ABSENCE, not a refusal — and the
    reason must name it as such, distinct from "no outcome receipt"."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _outcome_receipt("d-undecided", "unknown", "2026-09-06T10:00:00Z"),
    ])

    ok, reason = cil.is_deliverable_acceptable("d-undecided", receipts)

    assert ok is True
    assert "geen besliste uitkomst" in reason
    assert "unknown" in reason
    assert "afwezigheid is geen weigering" in reason
    assert "geen uitkomst-receipt" not in reason


@pytest.mark.parametrize("status", ["unknown", "no_signal", "", "   "])
def test_no_signal_and_empty_status_behave_like_unknown(tmp_path: Path, status: str) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-u", "claude", "2026-09-06T10:00:00Z"),
        _outcome_receipt("d-u", status, "2026-09-06T10:05:00Z"),
    ])

    ok, reason = cil.is_deliverable_acceptable("d-u", receipts)

    assert ok is False, f"status {status!r} must not resolve a contract_invalid chain"
    assert "contract_invalid" in reason


def test_missing_status_field_behaves_like_undecided(tmp_path: Path) -> None:
    """134 outcome receipts in the live ledger carry an empty status; a
    receipt with no status KEY at all must read the same way, not crash."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-nostatus", "claude", "2026-09-06T10:00:00Z"),
        {"dispatch_id": "d-nostatus", "event_type": "task_complete",
         "timestamp": "2026-09-06T10:05:00Z"},
    ])

    ok, _ = cil.is_deliverable_acceptable("d-nostatus", receipts)

    assert ok is False


@pytest.mark.parametrize("status", ["blocked", "completed", "in_progress",
                                    "done — awaiting ci + t0 gate"])
def test_unrecognized_status_does_not_heal_the_chain(tmp_path: Path, status: str) -> None:
    """ALLOWLIST, not a blocklist of {unknown, no_signal, empty}: these four
    literals all exist in the live ledger outside the enumerated set. A status
    this module has never seen makes no statement about the deliverable, so it
    must fail closed rather than silently unlock the merge."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-novel", "claude", "2026-09-06T10:00:00Z"),
        _outcome_receipt("d-novel", status, "2026-09-06T10:05:00Z"),
    ])

    ok, _ = cil.is_deliverable_acceptable("d-novel", receipts)

    assert ok is False


def test_contract_invalid_receipt_without_status_is_still_decided(tmp_path: Path) -> None:
    """``_is_decided_outcome`` short-circuits on the contract_invalid literal
    BEFORE the status allowlist, so an old ``report_contract_invalid`` record
    carrying no status field cannot drop out of its own check."""
    rec = {
        "dispatch_id": "legacy-ci",
        "event_type": "report_contract_invalid",
        "timestamp": "2026-06-03T08:51:09Z",
    }
    assert cil._is_decided_outcome(rec) is True

    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [rec])

    ok, reason = cil.is_deliverable_acceptable("legacy-ci", receipts)

    assert ok is False
    assert "contract_invalid" in reason


def test_decided_status_set_matches_measurement() -> None:
    """Pin the allowlist: measured 07-09 over 23.698 deliverable-outcome
    receipts in the live ledger. unknown (3897), no_signal (35) and the empty
    status (134) are the enumerated undecided ones and must stay out."""
    assert cil.DECIDED_OUTCOME_STATUSES == frozenset({
        "success", "done", "complete", "failed", "failure", "timeout",
        "contract_invalid",
    })
    for undecided in ("unknown", "no_signal", ""):
        assert undecided not in cil.DECIDED_OUTCOME_STATUSES


def test_undecided_receipts_do_not_block_a_dispatch_of_their_own(tmp_path: Path) -> None:
    """Skipped, not refused: turning 3897 unknown records into refusals of
    their own would be a merge blockade, not a fix."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _outcome_receipt("d-mixed", "unknown", "2026-09-06T10:00:00Z"),
        _success_receipt("d-mixed", "claude", "2026-09-06T10:05:00Z"),
        _outcome_receipt("d-mixed", "unknown", "2026-09-06T10:10:00Z"),
    ])

    ok, reason = cil.is_deliverable_acceptable("d-mixed", receipts)

    assert ok is True
    assert "not contract_invalid" in reason


# ---------------------------------------------------------------------------
# is_deliverable_acceptable: an unreadable ledger fails CLOSED
# ---------------------------------------------------------------------------

def test_unreadable_ledger_is_a_refusal_not_an_absence(tmp_path: Path) -> None:
    """A directory where the ledger should be: read raises, and the gate must
    refuse with the cause instead of degrading to an empty read (which reads
    as "no outcome receipt" and fails OPEN)."""
    receipts = tmp_path / "t0_receipts.ndjson"
    receipts.mkdir(parents=True)

    ok, reason = cil.is_deliverable_acceptable("d-any", receipts)

    assert ok is False
    assert "grootboek onleesbaar" in reason
    assert "afwezigheid" not in reason


def test_unreadable_ledger_permission_bit_is_a_refusal(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [_ci_receipt("d-any", "kimi", "2026-09-06T10:00:00Z")])
    receipts.chmod(0o000)
    try:
        if os.access(receipts, os.R_OK):  # running as root: chmod cannot deny
            pytest.skip("read permission not enforceable for this user")
        ok, reason = cil.is_deliverable_acceptable("d-any", receipts)
    finally:
        receipts.chmod(0o644)

    assert ok is False
    assert "grootboek onleesbaar" in reason


def test_strict_false_keeps_the_soft_read(tmp_path: Path) -> None:
    """The advisory contract is still available explicitly — strict=False
    degrades an unreadable ledger to an empty read, as before."""
    receipts = tmp_path / "t0_receipts.ndjson"
    receipts.mkdir(parents=True)

    ok, reason = cil.is_deliverable_acceptable("d-any", receipts, strict=False)

    assert ok is True
    assert "geen uitkomst-receipt" in reason


def test_advisory_readers_keep_soft_behaviour_on_unreadable_ledger(tmp_path: Path) -> None:
    """build_contract_invalid_summary / collect_contract_invalid_open surface
    at SessionStart — an I/O failure there must not crash a session."""
    receipts = tmp_path / "t0_receipts.ndjson"
    receipts.mkdir(parents=True)

    summary = cil.build_contract_invalid_summary(receipts)
    open_items = cil.collect_contract_invalid_open(receipts)

    assert summary["total"] == 0
    assert open_items == []


# ---------------------------------------------------------------------------
# evaluate_deliverable_acceptance: the MACHINE-READABLE reason (golf Bx, D4)
#
# is_deliverable_acceptable returns (False, <Dutch prose>) for three
# distinguishable cases — the real contract_invalid, an unreadable ledger, and
# an empty dispatch_id — and the merge door's --override-contract-invalid
# covered all three because prose is all it had to go on (OI-1666). The code
# is what the door decides on; the prose stays a message for a human.
# ---------------------------------------------------------------------------

def test_code_for_a_real_contract_invalid_chain(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [_ci_receipt("d-open", "kimi", "2026-09-06T10:00:00Z")])

    acceptance = cil.evaluate_deliverable_acceptance("d-open", receipts)

    assert acceptance.acceptable is False
    assert acceptance.code == cil.CODE_CONTRACT_INVALID
    assert "contract_invalid" in acceptance.reason


def test_code_for_an_unreadable_ledger_is_not_contract_invalid(tmp_path: Path) -> None:
    """The fail-closed I/O refusal is its OWN code — the whole point of D4:
    an operator who says "I know about that contract_invalid" must not thereby
    also wave through "I cannot read the ledger"."""
    receipts = tmp_path / "t0_receipts.ndjson"
    receipts.mkdir(parents=True)

    acceptance = cil.evaluate_deliverable_acceptance("d-any", receipts)

    assert acceptance.acceptable is False
    assert acceptance.code == cil.CODE_LEDGER_UNREADABLE
    assert acceptance.code != cil.CODE_CONTRACT_INVALID
    assert "grootboek onleesbaar" in acceptance.reason


def test_code_for_an_empty_dispatch_id(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"

    acceptance = cil.evaluate_deliverable_acceptance("", receipts)

    assert acceptance.acceptable is False
    assert acceptance.code == cil.CODE_EMPTY_DISPATCH_ID
    assert acceptance.code != cil.CODE_CONTRACT_INVALID


def test_codes_for_the_three_acceptable_outcomes(tmp_path: Path) -> None:
    """The accepting side is coded too: a caller that logs or branches on the
    code never has to re-derive "which kind of yes" from the prose."""
    healed = tmp_path / "healed.ndjson"
    _write_receipts(healed, [
        _ci_receipt("d-healed", "kimi", "2026-09-06T10:00:00Z"),
        _success_receipt("d-healed", "kimi", "2026-09-06T11:00:00Z"),
    ])
    absent = tmp_path / "absent.ndjson"
    _write_receipts(absent, [_ci_receipt("d-other", "kimi", "2026-09-06T10:00:00Z")])
    undecided = tmp_path / "undecided.ndjson"
    _write_receipts(undecided, [
        _ci_receipt("d-u", "kimi", "2026-09-06T10:00:00Z", status="unknown", report_path=None),
    ])

    assert cil.evaluate_deliverable_acceptance("d-healed", healed).code == (
        cil.CODE_NOT_CONTRACT_INVALID
    )
    assert cil.evaluate_deliverable_acceptance("never-ran", absent).code == (
        cil.CODE_NO_OUTCOME_RECEIPT
    )
    assert cil.evaluate_deliverable_acceptance("d-u", undecided).code == (
        cil.CODE_NO_DECIDED_OUTCOME
    )


def test_every_code_is_registered(tmp_path: Path) -> None:
    """ACCEPTANCE_CODES is the closed set a reader may switch on; a code
    returned but not registered would break that promise silently."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [_ci_receipt("d-open", "kimi", "2026-09-06T10:00:00Z")])

    for did, path in (("d-open", receipts), ("", receipts), ("nobody", receipts)):
        assert cil.evaluate_deliverable_acceptance(did, path).code in cil.ACCEPTANCE_CODES


def test_is_deliverable_acceptable_keeps_its_two_tuple_contract(tmp_path: Path) -> None:
    """The widening is additive: the existing (bool, str) callers are
    untouched and still get the same prose."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [_ci_receipt("d-open", "kimi", "2026-09-06T10:00:00Z")])

    ok, reason = cil.is_deliverable_acceptable("d-open", receipts)
    acceptance = cil.evaluate_deliverable_acceptance("d-open", receipts)

    assert (ok, reason) == (acceptance.acceptable, acceptance.reason)


def test_strict_false_is_carried_through_to_the_coded_form(tmp_path: Path) -> None:
    """strict=False still degrades an unreadable ledger to the soft read —
    the new entry point must not quietly harden the advisory contract."""
    receipts = tmp_path / "t0_receipts.ndjson"
    receipts.mkdir(parents=True)

    acceptance = cil.evaluate_deliverable_acceptance("d-any", receipts, strict=False)

    assert acceptance.acceptable is True
    assert acceptance.code == cil.CODE_NO_OUTCOME_RECEIPT


# ---------------------------------------------------------------------------
# build_t0_state.py wiring: full-state key + t0_index compact form
# ---------------------------------------------------------------------------

def test_build_t0_state_surfaces_contract_invalid_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _write_receipts(state_dir / "t0_receipts.ndjson", [
        _ci_receipt("d-1", "kimi", "2026-09-06T10:00:00Z"),
        _ci_receipt("d-2", "codex", "2026-09-06T11:00:00Z"),
        _success_receipt("d-3", "claude", "2026-09-06T11:30:00Z"),
    ])
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert "contract_invalid" in state
    ci = state["contract_invalid"]
    assert ci["total"] == 2
    assert ci["by_provider"] == {"kimi": 1, "codex": 1}


def test_build_t0_state_contract_invalid_empty_ledger_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert state["contract_invalid"]["total"] == 0
    assert state["contract_invalid"]["last_24h"] == 0


def test_build_t0_state_preserves_existing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Additive-only: contract_invalid must not disturb existing sections."""
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert "canonical_tracks" in state
    assert "human_gate_queue" in state
    assert "contract_invalid" in state


def test_t0_index_carries_compact_contract_invalid_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()
    _write_receipts(state_dir / "t0_receipts.ndjson", [
        _ci_receipt("d-1", "kimi", "2026-09-06T10:00:00Z"),
    ])

    state = bts.build_t0_state(state_dir, dispatch_dir)
    index = bts._build_t0_index(state)

    assert "contract_invalid" in index
    assert index["contract_invalid"]["total"] == 1
    assert len(index) <= 50
    assert len(json.dumps(index)) < 5 * 1024
