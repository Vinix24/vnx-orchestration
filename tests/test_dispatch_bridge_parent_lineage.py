"""tests/test_dispatch_bridge_parent_lineage.py — D2 (dlv-e5bdf6c502cf): fold a
fix-forward's declared work_ref into parent_dispatch.

Measured gap: only 37 of 1.365 receipts since 21-09 carried parent_dispatch
(2.7%) — without the chain, rework and first-pass yield can't be measured.
``stage_spec_bundle(parent_dispatch=...)`` and the receipt-side env fallback
(``append_receipt_internals/payload.py``) already existed; nothing ever
derived the field from a fix-forward's ``work_ref="dispatch/<X>"`` when the
caller left ``parent_dispatch`` unset. ``resolve_parent_dispatch`` closes
that gap; these tests pin it at the write path (the bytes the door/receipt
chain actually reads), not just the pure function.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_bridge  # noqa: E402

_GOOD_ID = "20260928-120000-followup"
_PARENT_ID = "20260927-090000-original"


def _stage(tmp_path, **over):
    base = dict(
        instruction_text="fix forward",
        dispatch_id=_GOOD_ID,
        role="dev",
        target_slot="T1",
        project_id="p1",
        provider="claude",
        data_dir=tmp_path,
    )
    base.update(over)
    return dispatch_bridge.stage_spec_bundle(**base)


def _payload(spec_file: Path) -> dict:
    return json.loads(spec_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# resolve_parent_dispatch — the pure function
# ---------------------------------------------------------------------------

class TestResolveParentDispatchPure:
    def test_derives_parent_from_work_ref(self):
        assert (
            dispatch_bridge.resolve_parent_dispatch(_GOOD_ID, f"dispatch/{_PARENT_ID}", None)
            == _PARENT_ID
        )

    def test_matching_explicit_parent_passes_through(self):
        assert (
            dispatch_bridge.resolve_parent_dispatch(
                _GOOD_ID, f"dispatch/{_PARENT_ID}", _PARENT_ID
            )
            == _PARENT_ID
        )

    def test_conflicting_explicit_parent_raises(self):
        with pytest.raises(ValueError, match="conflicts"):
            dispatch_bridge.resolve_parent_dispatch(
                _GOOD_ID, f"dispatch/{_PARENT_ID}", "some-other-dispatch"
            )

    def test_no_work_ref_returns_explicit_parent_unchanged(self):
        assert dispatch_bridge.resolve_parent_dispatch(_GOOD_ID, None, "explicit-parent") == "explicit-parent"

    def test_no_work_ref_and_no_parent_returns_none(self):
        assert dispatch_bridge.resolve_parent_dispatch(_GOOD_ID, None, None) is None

    def test_work_ref_naming_own_branch_is_not_a_parent(self):
        assert dispatch_bridge.resolve_parent_dispatch(_GOOD_ID, f"dispatch/{_GOOD_ID}", None) is None

    def test_work_ref_without_dispatch_shape_is_not_a_parent(self):
        assert dispatch_bridge.resolve_parent_dispatch(_GOOD_ID, "feature/some-branch", None) is None

    def test_work_ref_strips_origin_prefix_before_matching(self):
        assert (
            dispatch_bridge.resolve_parent_dispatch(_GOOD_ID, f"origin/dispatch/{_PARENT_ID}", None)
            == _PARENT_ID
        )

    def test_work_ref_strips_refs_heads_prefix_before_matching(self):
        assert (
            dispatch_bridge.resolve_parent_dispatch(
                _GOOD_ID, f"refs/heads/dispatch/{_PARENT_ID}", None
            )
            == _PARENT_ID
        )


# ---------------------------------------------------------------------------
# stage_spec_bundle — the write path (a): derived parent lands ON DISK
# ---------------------------------------------------------------------------

def test_stage_derives_parent_dispatch_from_work_ref_on_disk(tmp_path):
    """The red/green pair for D2: a fix-forward stages with work_ref="dispatch/X"
    and no explicit parent. On the OLD code this wrote parent_dispatch=None
    (only 2.7% of receipts carried the field); on the FIXED code it derives X."""
    spec_file = _stage(tmp_path, work_ref=f"dispatch/{_PARENT_ID}")
    payload = _payload(spec_file)
    assert payload["parent_dispatch"] == _PARENT_ID
    assert payload["work_ref"] == f"dispatch/{_PARENT_ID}"


def test_stage_conflicting_explicit_parent_is_refused(tmp_path):
    """(b) An explicit parent that disagrees with the work_ref-derived predecessor
    is always a caller bug (two lineages for one bundle) — refuse loud, write
    nothing."""
    with pytest.raises(ValueError, match="conflicts"):
        _stage(
            tmp_path,
            work_ref=f"dispatch/{_PARENT_ID}",
            parent_dispatch="some-other-dispatch",
        )
    # nothing staged: the bundle dir may exist (mkdir happens after the check —
    # here it happens BEFORE any write since resolution runs first) but no spec file.
    assert not (tmp_path / "dispatches" / "pending" / _GOOD_ID / "dispatch-spec.json").exists()


def test_stage_matching_explicit_parent_is_accepted(tmp_path):
    spec_file = _stage(
        tmp_path, work_ref=f"dispatch/{_PARENT_ID}", parent_dispatch=_PARENT_ID,
    )
    assert _payload(spec_file)["parent_dispatch"] == _PARENT_ID


def test_stage_first_dispatch_with_pr_id_and_no_work_ref_gets_no_parent(tmp_path):
    """(c) A first dispatch that already carries a pr_id (e.g. a review-round
    followup on its OWN PR) but no work_ref is not a fix-forward — no lineage is
    enforced or invented from pr_id alone."""
    spec_file = _stage(tmp_path, pr_id="1234")
    payload = _payload(spec_file)
    assert payload["pr_id"] == "1234"
    assert payload["parent_dispatch"] is None


def test_stage_work_ref_equal_to_own_branch_gets_no_parent(tmp_path):
    """(d) work_ref naming this dispatch's OWN branch is not a continuation of
    anything — no lineage."""
    spec_file = _stage(tmp_path, work_ref=f"dispatch/{_GOOD_ID}")
    assert _payload(spec_file)["parent_dispatch"] is None


def test_stage_normal_dispatch_no_work_ref_no_parent(tmp_path):
    spec_file = _stage(tmp_path)
    assert _payload(spec_file)["parent_dispatch"] is None


# ---------------------------------------------------------------------------
# (e) escalation route stays unaffected — it sets parent_dispatch explicitly and
# never sets work_ref, so it never hits the derivation/conflict branches at all.
# ---------------------------------------------------------------------------

def test_escalation_route_still_stages_its_own_parent_dispatch(tmp_path, monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/local/bin/kimi")

    rejected_id = "20260815-094500-rejected-attempt"
    spec_file = dispatch_bridge.stage_escalation_bundle(
        rejected_dispatch_id=rejected_id,
        tier_from="tier-low",
        failure_class="model_error",
        instruction_text="escalate: the cheap attempt was rejected",
        dispatch_id="20260815-100000-escalated-followup",
        role="dev",
        target_slot="T1",
        project_id="p1",
        data_dir=tmp_path,
        env={},
        state_dir=tmp_path / "state",
        now=0.0,
    )
    payload = _payload(spec_file)
    assert payload["parent_dispatch"] == rejected_id
    assert payload["work_ref"] is None


# ---------------------------------------------------------------------------
# (a, receipt-path half): the door exports the resolved parent_dispatch as
# VNX_PARENT_DISPATCH, and a worker-authored receipt without its own explicit
# parent_dispatch inherits it via the same fallback test_model_ssot_and_chainlink
# already pins (append_receipt_internals.payload._stamp_model_identity).
# ---------------------------------------------------------------------------

def test_receipt_path_inherits_the_derived_parent_dispatch(tmp_path, monkeypatch):
    """The env-export block that feeds the chain-link fallback
    (``VNX_PARENT_DISPATCH`` -> ``append_receipt_internals.payload``, pinned by
    ``test_model_ssot_and_chainlink.TestChainLink``) only runs on a REAL
    (non-dry-run) fire — dry-run returns before it. So this test fires for
    real, with the lane executor mocked out (never a real provider/claude
    process) exactly like ``test_dispatch_cli.test_claude_routes_to_headless``
    does."""
    from unittest.mock import patch

    from dispatch_cli import run_dispatch
    from append_receipt_internals.payload import append_receipt_payload

    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.delenv("VNX_PARENT_DISPATCH", raising=False)

    followup_id = "20260928-130000-fixforward"
    spec_file = dispatch_bridge.stage_spec_bundle(
        instruction_text=(
            "# Fix-forward\n\nRole: backend-developer\n\nContinue the prior attempt.\n"
        ),
        dispatch_id=followup_id,
        role="backend-developer",
        target_slot="T1",
        project_id="vnx-dev",
        provider="claude",
        data_dir=data_dir,
        work_ref=f"dispatch/{_PARENT_ID}",
    )
    assert _payload(spec_file)["parent_dispatch"] == _PARENT_ID

    # build_runtime_snapshot runs for REAL (unmocked) — it is the function that
    # derives snapshot.parent_dispatch from spec.parent_dispatch (dispatch_cli.py
    # ~:2457), which dispatch_plan.compile_plan then carries onto plan.parent_dispatch
    # (dispatch_plan.py:468). Only the lane EXECUTOR is mocked, so no real
    # provider/claude process ever spawns.
    with patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_exec:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_exec.assert_called_once()

    import os
    assert os.environ.get("VNX_PARENT_DISPATCH") == _PARENT_ID

    rf = tmp_path / "t0_receipts.ndjson"
    append_receipt_payload(
        {
            "timestamp": "2026-09-28T00:00:00Z",
            "event_type": "task_complete",
            "dispatch_id": followup_id,
            "terminal": "T0",
            "status": "success",
            "receipt_kind": "dispatch",
            "model": "sonnet-5",
        },
        receipts_file=str(rf),
        skip_enrichment=True,
    )
    line = json.loads(rf.read_text().splitlines()[-1])
    assert line["parent_dispatch"] == _PARENT_ID
