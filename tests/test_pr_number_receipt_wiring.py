"""test_pr_number_receipt_wiring.py — PRD bewijsketen fase 1, F1-1: the PR
number a dispatch's own push+PR enforcement resolves must survive into the
receipt written to the ledger.

T0 measured (2026-09-05, 5,926 LIVE task_complete/non-pytest receipts):
pr_number 0.0%, pr_link 0.0%, pr_id 1.4%. Two independent defects mask each
other:

  1. ``envelope_types._AdapterResult`` (the value dispatch_envelope.
     _enforce_push_pr and envelope_govern._govern pass between EXECUTE and
     GOVERN) had no field to carry a PR number resolved DURING this
     dispatch's own run — only ``EnvelopeSpec.pr_id``, a pre-known
     fix-forward target set at dispatch-creation time, ever reached the
     receipt (the source of the measured 1.4%).
  2. ``dispatch_envelope._enforce_push_pr`` discarded
     ``pr_enforcement.PrEnforcementResult.pr_number`` on the success path —
     even once the schema HAD a field for it, nothing fed it.

This file proves both are fixed: ``_enforce_push_pr`` threads the resolved
number onto the ``_AdapterResult`` it returns, and ``_govern`` stamps it onto
the receipt's ``pr_id`` field (reused — see envelope_types.py's
``_AdapterResult.pr_number`` docstring for why reuse over a new field) when
no pre-known ``spec.pr_id`` already claims that slot.

Mirrors tests/test_lane_matrix_row7_push_pr.py's and
tests/test_receipt_v2_pr4_wiring.py's fixture patterns (this file does not
import from either — it isolates ``_enforce_push_pr``/``_govern`` directly
rather than driving the full run_envelope_headless_plan worktree scaffolding,
since the discarded-value bug lives entirely inside those two functions).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dispatch_envelope  # noqa: E402
from envelope_types import EnvelopeSpec, _AdapterResult  # noqa: E402
from envelope_govern import _govern  # noqa: E402
from pr_enforcement import PrEnforcementResult  # noqa: E402


def _read_lines(receipts_path: Path) -> list:
    if not receipts_path.exists():
        return []
    return [json.loads(l) for l in receipts_path.read_text().splitlines() if l.strip()]


def _spec(tmp_path: Path, *, pr_id=None, dispatch_id: str) -> EnvelopeSpec:
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "unified_reports").mkdir(parents=True, exist_ok=True)
    return EnvelopeSpec(
        dispatch_id=dispatch_id,
        terminal_id="T1",
        provider="claude",
        model="claude-sonnet-5",
        instruction="do the work",
        role=None,
        pr_id=pr_id,
        state_dir=state_dir,
        data_dir=data_dir,
    )


def _success_result(**overrides) -> _AdapterResult:
    base = dict(returncode=0, completion_text="done", status="success")
    base.update(overrides)
    return _AdapterResult(**base)


# ---------------------------------------------------------------------------
# Unit: _enforce_push_pr threads the resolved PR number onto the result
# ---------------------------------------------------------------------------


def test_enforce_push_pr_threads_resolved_pr_number_onto_result(tmp_path):
    """Before the fix, a successful enforce_pr_exists() call's pr_number was
    logged and then discarded — the returned _AdapterResult was untouched.
    """
    result_in = _success_result()

    fake_pr_result = PrEnforcementResult(
        applicable=True, ok=True, pushed=True, pr_number=4242, created=True,
    )

    with patch("dispatch_worktree_isolation.read_worktree_base_sha",
               return_value=("d" * 40, None)), \
         patch("tmux_worktree.resolve_effective_branch",
               side_effect=lambda *, wt, expected_branch, dispatch_id: expected_branch), \
         patch("tmux_worktree.classify_path", return_value="pushed"), \
         patch("pr_enforcement.enforce_pr_exists", return_value=fake_pr_result):
        result_out = dispatch_envelope._enforce_push_pr(
            dispatch_id="pr-number-unit-test",
            branch="dispatch/pr-number-unit-test",
            wt_path=tmp_path / "wt",
            repo_root=tmp_path,
            receipts_file=tmp_path / "t0_receipts.ndjson",
            result=result_in,
        )

    assert result_out.pr_number == 4242, (
        f"_enforce_push_pr must thread the resolved PR number onto the "
        f"_AdapterResult it returns; got pr_number={result_out.pr_number!r}"
    )
    # Unrelated fields must pass through untouched.
    assert result_out.status == "success"
    assert result_out.completion_text == "done"


def test_enforce_push_pr_leaves_result_unchanged_when_not_applicable(tmp_path):
    """clean worktree: enforce_pr_exists returns applicable=False — nothing
    to thread, the result must be returned untouched (not even a no-op copy
    that could mask a future field getting dropped)."""
    result_in = _success_result()
    fake_pr_result = PrEnforcementResult(applicable=False, ok=True, reason="clean")

    with patch("dispatch_worktree_isolation.read_worktree_base_sha",
               return_value=("d" * 40, None)), \
         patch("tmux_worktree.resolve_effective_branch",
               side_effect=lambda *, wt, expected_branch, dispatch_id: expected_branch), \
         patch("tmux_worktree.classify_path", return_value="clean"), \
         patch("pr_enforcement.enforce_pr_exists", return_value=fake_pr_result):
        result_out = dispatch_envelope._enforce_push_pr(
            dispatch_id="pr-number-unit-test-clean",
            branch="dispatch/pr-number-unit-test-clean",
            wt_path=tmp_path / "wt",
            repo_root=tmp_path,
            receipts_file=tmp_path / "t0_receipts.ndjson",
            result=result_in,
        )

    assert result_out is result_in
    assert result_out.pr_number is None


# ---------------------------------------------------------------------------
# Behavior: _govern stamps pr_id from the resolved pr_number — the RED test.
#
# Before the fix, _AdapterResult had no pr_number field at all: constructing
# `_success_result(pr_number=4242)` below raised
# `TypeError: __init__() got an unexpected keyword argument 'pr_number'` —
# the field did not survive the schema step because the schema (the
# dataclass _govern reads from) did not carry it, by construction.
# ---------------------------------------------------------------------------


def test_govern_stamps_pr_id_from_resolved_pr_number_when_spec_pr_id_absent(tmp_path):
    spec = _spec(tmp_path, pr_id=None, dispatch_id="pr-number-govern-test")
    adapter_result = _success_result(pr_number=4242)
    now = datetime.now(timezone.utc)

    report_path, receipt_path = _govern(spec, adapter_result, now, now)

    assert receipt_path is not None, "GOVERN must have written a receipt"
    lines = _read_lines(spec.state_dir / "t0_receipts.ndjson")
    assert lines, "no receipt line was written"
    assert lines[-1]["pr_id"] == "4242", (
        f"the PR number resolved by push+PR enforcement this run must survive "
        f"into the receipt's pr_id field; got {lines[-1].get('pr_id')!r} in "
        f"{lines[-1]!r}"
    )


def test_govern_prefers_spec_pr_id_over_resolved_pr_number(tmp_path):
    """A pre-known fix-forward target (spec.pr_id) always wins — the two
    sources are not expected to disagree in practice (skip_pr leaves
    pr_number None for exactly the dispatches that carry a spec.pr_id), but
    the precedence must be deterministic and must never let a resolved
    number silently overwrite an explicit target."""
    spec = _spec(tmp_path, pr_id="1000", dispatch_id="pr-number-govern-precedence")
    adapter_result = _success_result(pr_number=4242)
    now = datetime.now(timezone.utc)

    _report_path, receipt_path = _govern(spec, adapter_result, now, now)

    assert receipt_path is not None
    lines = _read_lines(spec.state_dir / "t0_receipts.ndjson")
    assert lines[-1]["pr_id"] == "1000"


def test_govern_leaves_pr_id_none_when_neither_source_present(tmp_path):
    spec = _spec(tmp_path, pr_id=None, dispatch_id="pr-number-govern-absent")
    adapter_result = _success_result()  # pr_number defaults to None
    now = datetime.now(timezone.utc)

    _report_path, receipt_path = _govern(spec, adapter_result, now, now)

    assert receipt_path is not None
    lines = _read_lines(spec.state_dir / "t0_receipts.ndjson")
    assert lines[-1]["pr_id"] is None
