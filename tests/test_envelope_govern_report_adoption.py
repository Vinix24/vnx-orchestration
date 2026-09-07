"""test_envelope_govern_report_adoption.py — fix-forward on PR #1802:
the headless envelope lane adopts a stray worker report before synthesising
over it, exactly like dispatch_govern already does for the tmux/subprocess
lanes.

Measured 2026-09-06 (head e156e543): five real worker reports on the
claude_headless lane landed outside the central store while envelope_govern.
_govern() wrote the generic ## Response wrapper over them — the wrapper then
failed the report-body contract (missing ## Summary/## Changes/
## Verification/## Open Items) and the receipt status was overridden to
"contract_invalid" even though a perfectly good worker report existed on
disk. dispatch_govern._resolve_worker_report_with_adoption already covered
this for the tmux/subprocess lanes (A-bis-2); this fix wires the SAME shared
helper into envelope_govern._govern, which never called it.

Without the fix: a valid report sitting at the worktree path or the outer
consumer-checkout path is invisible to _govern() — it emits the generic
wrapper and the receipt carries no report_relocated warning.

With the fix: _govern() searches central -> worktree -> repo-root (consumer
project root) BEFORE emitting the wrapper; a validated find is relocated to
the central path and the receipt's warnings[] carries a report_relocated
entry naming the original stray path.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from dispatch_worktree_isolation import _dispatch_worktree_dir  # noqa: E402
from envelope_govern import _govern  # noqa: E402
from envelope_types import EnvelopeSpec, _AdapterResult  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def spec(tmp_path):
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    data_dir.mkdir()
    state_dir.mkdir()
    return EnvelopeSpec(
        dispatch_id="test-envelope-adopt-001",
        terminal_id="T1",
        provider="claude",
        model="sonnet",
        instruction="do the thing",
        role="backend-developer",
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
    )


@pytest.fixture()
def success_result():
    return _AdapterResult(
        returncode=0,
        completion_text="Worker output without contract headings.",
        status="success",
    )


@pytest.fixture()
def consumer_root(tmp_path):
    root = tmp_path / "consumer-repo"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_body(marker: str = "Implemented the feature correctly") -> str:
    return (
        "## Summary\n\n"
        f"{marker} with full test coverage. All tests pass and the "
        "implementation is complete and correct in every regard.\n\n"
        "## Changes\n\n- Implemented feature X\n\n"
        "## Verification\n\n- pytest passed: 5/5\n\n"
        "## Open Items\n\nNone\n"
    )


def _run_govern(spec, result, consumer_root):
    """Run _govern() with the emission/receipt seam mocked exactly like
    test_envelope_govern_contract_enforce.py, EXCEPT emit_unified_report is
    left REAL (unmocked): the mechanism under test is emit_unified_report's
    own idempotent early-return picking up the file _govern's adoption step
    already relocated to the central path, and that can only be observed by
    letting the real function run.
    """
    receipts_file = spec.state_dir / "t0_receipts.ndjson"
    receipts_file.write_text("")

    mock_emit_receipt = MagicMock(return_value=receipts_file)

    with patch(
        "envelope_govern._archive_dispatch_events", return_value=("", True)
    ), patch(
        "envelope_govern._clear_dispatch_events"
    ), patch(
        "envelope_govern._receipt_exists_for_dispatch", return_value=False
    ), patch(
        "dispatch_worktree_isolation.resolve_consumer_project_root",
        return_value=consumer_root,
    ), patch(
        "governance_emit.emit_dispatch_receipt", mock_emit_receipt
    ):
        report_path, _receipt_path = _govern(
            spec,
            result,
            start_time=datetime(2026, 9, 7, 12, 0, 0),
            end_time=datetime(2026, 9, 7, 12, 1, 0),
        )

    return report_path, mock_emit_receipt


# ---------------------------------------------------------------------------
# Tests — stray-report adoption
# ---------------------------------------------------------------------------


class TestEnvelopeGovernReportAdoption:

    def test_adopts_stray_report_from_repo_root(self, spec, success_result, consumer_root):
        """A valid worker report sitting at <consumer_root>/.vnx-data/
        unified_reports/<id>.md with nothing central and nothing at the
        worktree path is adopted: the central file gets the authored body,
        the stray original is gone, and the receipt records a
        report_relocated warning."""
        stray_dir = consumer_root / ".vnx-data" / "unified_reports"
        stray_dir.mkdir(parents=True)
        stray_path = stray_dir / f"{spec.dispatch_id}.md"
        stray_path.write_text(_valid_body(), encoding="utf-8")

        report_path, mock_emit_receipt = _run_govern(spec, success_result, consumer_root)

        central_path = spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md"
        assert report_path == central_path
        assert central_path.exists()
        assert "Implemented the feature correctly" in central_path.read_text(encoding="utf-8")
        assert not stray_path.exists(), "stray original must be relocated, not copied"

        call_kwargs = mock_emit_receipt.call_args.kwargs
        warnings = call_kwargs.get("warnings") or []
        assert any(
            w.get("code") == "report_relocated" and str(stray_path) in w.get("message", "")
            for w in warnings
        ), f"expected a report_relocated warning naming {stray_path}, got {warnings!r}"
        # The relocated report passes the body contract — no
        # report_contract_violated entry, and status is not downgraded.
        assert not any(w.get("code") == "report_contract_violated" for w in warnings)
        assert call_kwargs["status"] == "success"

    def test_adopts_stray_report_from_worktree_path(self, spec, success_result, consumer_root):
        """Same adoption, but the stray report sits at the ephemeral
        per-dispatch worktree path (the same path run_envelope_headless_plan
        derives via dispatch_worktree_isolation._dispatch_worktree_dir)."""
        worktree = _dispatch_worktree_dir(consumer_root, spec.dispatch_id)
        stray_dir = worktree / ".vnx-data" / "unified_reports"
        stray_dir.mkdir(parents=True)
        stray_path = stray_dir / f"{spec.dispatch_id}.md"
        stray_path.write_text(_valid_body("WORKTREE body marker"), encoding="utf-8")

        report_path, mock_emit_receipt = _run_govern(spec, success_result, consumer_root)

        central_path = spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md"
        assert report_path == central_path
        assert "WORKTREE body marker" in central_path.read_text(encoding="utf-8")
        assert not stray_path.exists(), "stray original must be relocated, not copied"

        call_kwargs = mock_emit_receipt.call_args.kwargs
        warnings = call_kwargs.get("warnings") or []
        assert any(
            w.get("code") == "report_relocated" and str(stray_path) in w.get("message", "")
            for w in warnings
        )

    def test_worktree_report_wins_over_repo_root(self, spec, success_result, consumer_root):
        """Search order: the worktree path is consulted before repo-root.
        When both hold a valid report, the worktree one is adopted and the
        repo-root one is left untouched."""
        worktree = _dispatch_worktree_dir(consumer_root, spec.dispatch_id)
        worktree_stray_dir = worktree / ".vnx-data" / "unified_reports"
        worktree_stray_dir.mkdir(parents=True)
        worktree_stray = worktree_stray_dir / f"{spec.dispatch_id}.md"
        worktree_stray.write_text(_valid_body("WORKTREE body marker"), encoding="utf-8")

        repo_root_stray_dir = consumer_root / ".vnx-data" / "unified_reports"
        repo_root_stray_dir.mkdir(parents=True)
        repo_root_stray = repo_root_stray_dir / f"{spec.dispatch_id}.md"
        repo_root_stray.write_text(_valid_body("REPO ROOT body marker"), encoding="utf-8")

        report_path, _mock_emit_receipt = _run_govern(spec, success_result, consumer_root)

        content = report_path.read_text(encoding="utf-8")
        assert "WORKTREE body marker" in content
        assert "REPO ROOT body marker" not in content
        assert not worktree_stray.exists(), "the adopted worktree stray must be relocated"
        assert repo_root_stray.exists(), "the un-adopted repo_root stray must be left in place"

    def test_invalid_repo_root_stray_report_still_synthesizes(
        self, spec, success_result, consumer_root,
    ):
        """An INVALID stray report at repo_root falls through to the
        existing generic-wrapper synthesis exactly as before (regression
        guard) — and is left in place, not moved."""
        stray_dir = consumer_root / ".vnx-data" / "unified_reports"
        stray_dir.mkdir(parents=True)
        stray_path = stray_dir / f"{spec.dispatch_id}.md"
        stray_path.write_text("not a valid report body\n", encoding="utf-8")

        report_path, mock_emit_receipt = _run_govern(spec, success_result, consumer_root)

        assert stray_path.exists(), "an invalid stray report must not be relocated"
        assert stray_path.read_text(encoding="utf-8") == "not a valid report body\n"
        # Falls through to the generic ## Response wrapper, which fails the
        # body contract — same pre-existing contract_invalid override.
        content = report_path.read_text(encoding="utf-8")
        assert "## Response" in content
        call_kwargs = mock_emit_receipt.call_args.kwargs
        warnings = call_kwargs.get("warnings") or []
        assert any(w.get("code") == "report_contract_violated" for w in warnings)
        assert call_kwargs["status"] == "contract_invalid"

    def test_no_stray_report_anywhere_falls_through_unchanged(
        self, spec, success_result, consumer_root,
    ):
        """Regression: nothing at central, worktree, or repo-root — _govern
        must behave exactly as before this fix (generic wrapper, no
        report_relocated warning)."""
        report_path, mock_emit_receipt = _run_govern(spec, success_result, consumer_root)

        content = report_path.read_text(encoding="utf-8")
        assert "## Response" in content
        call_kwargs = mock_emit_receipt.call_args.kwargs
        warnings = call_kwargs.get("warnings") or []
        assert not any(w.get("code") == "report_relocated" for w in warnings)
