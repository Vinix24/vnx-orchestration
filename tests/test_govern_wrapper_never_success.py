"""tests/test_govern_wrapper_never_success.py — OI-1637 hard invariant.

A report that is a wrapper (envelope_govern.py's generic ``## Response``
fallback) or a governance-fabricated body (dispatch_govern.py's
``_synthesize()``) must NEVER leave a receipt with status "success" or
"done" on the ledger.

Measured population (claudedocs/2026-09-05-golf2-contractpercentage-per-lane.md,
section 5a): 26 wrapper reports across kimi/deepseek-harness/claude carried a
"success" receipt anyway. Re-measuring against the live central store
(2026-09-06) shows 37 of those cases predate the 2026-08-09 binding-contract
commit (c28b7ac4, OI-1017/OI-1048) or are unit-test dispatch_id pollution in
the shared store ("test-*"), and the kimi/deepseek-harness mechanism that
DOES correct most wrapper reports (report_to_receipt_converter.py's async
``report_contract_invalid`` scan) sits in ``provider_dispatch.py`` /
``report_to_receipt_converter.py`` — outside this dispatch's file scope
(neither this worker's nor any listed sibling worker's). See the ADR and the
dispatch report's "Open Items" for that scope boundary.

What IS in scope and reproducible today: envelope_govern.py's own idempotent-
dedup skip (``_receipt_exists_for_dispatch``, added for deliver_with_recovery's
legacy safety-net receipt — see envelope_govern.py's module docstring) runs
BEFORE the contract-invalid downgrade computed above it ever gets persisted.
If a receipt already exists for a dispatch_id (a retried dispatch, or a
legacy dual-write), _govern() silently accepts whatever that earlier record
claims — even a "success" the just-read report body disproves. Test 1 below
reproduces exactly this route, byte-faithful in shape to the wrapper form
governance_emit.emit_unified_report writes (``# Dispatch <id>`` + identity
block + ``## Response``), anonymized to a synthetic dispatch id.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from envelope_govern import _govern
from envelope_types import EnvelopeSpec, _AdapterResult
from report_body_contract import CONTRACT_INVALID_STATUS


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def spec(tmp_path):
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    data_dir.mkdir()
    state_dir.mkdir()
    return EnvelopeSpec(
        dispatch_id="anon-wrapper-success-001",
        terminal_id="T1",
        provider="kimi",
        model="kimi-k3",
        instruction="do the thing",
        role="backend-developer",
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
    )


def _write_wrapper_report(data_dir: Path, dispatch_id: str) -> Path:
    """Byte-faithful (anonymized) reproduction of the envelope's generic
    wrapper — governance_emit.emit_unified_report's shape when no worker
    report exists: identity block + a single ## Response section. This is
    what a report on the contract path looks like when the worker delivered
    NOTHING — the exact shape 12 kimi / 10 deepseek-harness / 4 claude
    dispatches carried alongside a "success" receipt (golf-2, section 5a)."""
    reports_dir = data_dir / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{dispatch_id}.md"
    report_path.write_text(
        f"# Dispatch {dispatch_id}\n\n"
        f"**Dispatch-ID**: {dispatch_id}\n"
        f"**Provider**: kimi\n"
        f"**Terminal**: T1\n"
        f"**Duration**: 42.0s\n\n"
        f"## Response\n\n(no response captured)\n",
        encoding="utf-8",
    )
    return report_path


def _run_govern_with_mocks(spec, result, *, report_path, receipt_already_exists: bool):
    """Run _govern() with the same minimal-mock pattern as
    test_envelope_govern_contract_enforce.py, but with control over whether
    an earlier receipt already exists for this dispatch_id (the idempotent-
    dedup branch this test targets)."""
    receipt_path = spec.state_dir / "t0_receipts.ndjson"
    receipt_path.write_text("")

    mock_emit_report = MagicMock(return_value=report_path)
    mock_emit_receipt = MagicMock(return_value=receipt_path)

    with patch(
        "envelope_govern._archive_dispatch_events", return_value=("", True)
    ), patch(
        "envelope_govern._clear_dispatch_events"
    ), patch(
        "envelope_govern._receipt_exists_for_dispatch", return_value=receipt_already_exists
    ), patch(
        "governance_emit.emit_unified_report", mock_emit_report
    ), patch(
        "governance_emit.emit_dispatch_receipt", mock_emit_receipt
    ):
        _govern(
            spec,
            result,
            start_time=datetime(2026, 9, 6, 12, 0, 0),
            end_time=datetime(2026, 9, 6, 12, 1, 0),
        )

    return mock_emit_receipt


# ---------------------------------------------------------------------------
# Test 1 — the hard invariant, reproduced via the idempotent-dedup route
# ---------------------------------------------------------------------------


class TestWrapperNeverSurvivesAsSuccess:
    def test_wrapper_report_with_preexisting_receipt_is_corrected_not_skipped(
        self, spec,
    ):
        """A wrapper report (worker delivered nothing) whose dispatch_id
        ALREADY has a receipt on the ledger (a retried dispatch, or
        deliver_with_recovery's legacy safety-net write — envelope_govern.py's
        own module docstring) must still get its contract-invalid downgrade
        PERSISTED — not silently dropped by the idempotent-dedup skip.

        Before the fix: _receipt_exists_for_dispatch()==True short-circuits
        the whole receipt-emit block, so emit_dispatch_receipt is never
        called at all — the earlier (possibly "success") record stands as
        the ledger's only word, uncorrected, forever.
        """
        report_path = _write_wrapper_report(spec.data_dir, spec.dispatch_id)
        success_result = _AdapterResult(
            returncode=0, completion_text="", status="success",
        )

        mock_emit_receipt = _run_govern_with_mocks(
            spec, success_result, report_path=report_path,
            receipt_already_exists=True,
        )

        assert mock_emit_receipt.called, (
            "a wrapper report must trigger a corrective receipt emit even "
            "when an earlier receipt already exists for this dispatch_id — "
            "the idempotent-dedup skip must not swallow the contract-invalid "
            "downgrade this governance pass just computed"
        )
        call_kwargs = mock_emit_receipt.call_args.kwargs
        assert call_kwargs["status"] != "success", (
            f"a wrapper report (worker delivered nothing) must never leave a "
            f"'success' receipt as the ledger's last word, got "
            f"status={call_kwargs['status']!r}"
        )
        assert call_kwargs["status"] == CONTRACT_INVALID_STATUS

    def test_wrapper_report_with_no_preexisting_receipt_is_contract_invalid(
        self, spec,
    ):
        """Baseline (no idempotent-dedup involved): a wrapper report with
        adapter-claimed success is downgraded to contract_invalid — this
        already worked before this dispatch (envelope_govern.py:256) and
        must keep working."""
        report_path = _write_wrapper_report(spec.data_dir, spec.dispatch_id)
        success_result = _AdapterResult(
            returncode=0, completion_text="", status="success",
        )

        mock_emit_receipt = _run_govern_with_mocks(
            spec, success_result, report_path=report_path,
            receipt_already_exists=False,
        )

        assert mock_emit_receipt.called
        assert mock_emit_receipt.call_args.kwargs["status"] == CONTRACT_INVALID_STATUS

    def test_authored_report_with_preexisting_receipt_still_skips(self, spec):
        """Sanity: a CONTRACT-COMPLIANT report must still take the ordinary
        idempotent-dedup skip when a receipt already exists — this fix must
        not turn every retried dispatch into a double-emit."""
        reports_dir = spec.data_dir / "unified_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"{spec.dispatch_id}.md"
        report_path.write_text(
            "## Summary\n\n" + ("x" * 60) + "\n\n"
            "## Changes\n\n- did the thing\n\n"
            "## Verification\n\n- pytest: 3 passed\n\n"
            "## Open Items\n\nNone\n",
            encoding="utf-8",
        )
        success_result = _AdapterResult(
            returncode=0, completion_text="", status="success",
        )

        mock_emit_receipt = _run_govern_with_mocks(
            spec, success_result, report_path=report_path,
            receipt_already_exists=True,
        )

        assert not mock_emit_receipt.called, (
            "a contract-compliant report must not trigger a corrective "
            "receipt emit — the ordinary idempotent-dedup skip still applies"
        )


# ---------------------------------------------------------------------------
# Test 3 — dispatch_govern.py side of the same invariant (the ADR's chosen
# direction: option (b), abolish the fake-compliant synthesis shape)
# ---------------------------------------------------------------------------


def test_dispatch_govern_synthesized_report_never_yields_done_or_success(tmp_path):
    """A synthesized report (no worker report at all — dispatch_govern's own
    fabricated body) must NEVER produce a receipt status of "done" or
    "success". Post-ADR, the value is CONTRACT_INVALID_STATUS — the same
    canonical value envelope_govern.py's lanes already use for this exact
    situation, not the pre-ADR "failed" (still correct for the "never
    success" invariant, but a different label than the rest of the chain
    uses for "no valid report")."""
    from dispatch_govern import GovernRaw, GovernSpec, govern

    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    data_dir.mkdir()
    state_dir.mkdir()

    spec = GovernSpec(
        dispatch_id="anon-synth-never-success-001",
        terminal_id="T1",
        instruction="do the thing",
        data_dir=data_dir,
        state_dir=state_dir,
        model="sonnet",
    )
    # receipt=None -> no worker receipt -> _synthesize() path (no authored report on disk).
    raw = GovernRaw(receipt=None, duration_seconds=42.0)

    with patch("dispatch_govern._git_summary", return_value="No commit; timeout."), \
         patch("dispatch_govern._git_changes", return_value="No git diff available"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    assert outcome.contract_status == "synthesized", (
        f"a body the governance layer fabricated itself must stay classified "
        f"'synthesized', not relabeled 'violated' — got {outcome.contract_status!r}"
    )

    receipts_file = state_dir / "t0_receipts.ndjson"
    assert receipts_file.exists()
    import json
    lines = [json.loads(l) for l in receipts_file.read_text().splitlines() if l.strip()]
    synthesized = [r for r in lines if r.get("source") == "tmux_interactive_lane_synthesized"]
    assert len(synthesized) == 1
    status = synthesized[0]["status"]
    assert status not in ("done", "success"), (
        f"a synthesized (no-report) dispatch must never yield a 'done'/'success' "
        f"receipt, got status={status!r}"
    )
    assert status == CONTRACT_INVALID_STATUS, (
        f"expected the canonical {CONTRACT_INVALID_STATUS!r} (the value "
        f"envelope_govern.py's lanes already use for this case), got {status!r}"
    )
