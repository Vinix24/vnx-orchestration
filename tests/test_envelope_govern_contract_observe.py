"""test_envelope_govern_contract_observe.py — OI-1017/OI-1048 L2: observable
body-contract validation on envelope lanes.

Verifies that _govern() emits a warnings[] entry with a filterable
code when the emitted unified report fails the body contract, while
NOT changing the receipt status (observable mode, not yet binding).

Without the fix:
- _govern() writes a report and receipt without body-contract validation
- A report missing the four mandatory headings produces a success receipt
- No warning is logged

With the fix:
- _govern() validates the report body after emit_unified_report
- On violation, a warnings[] entry with code="report_contract_violated"
  is appended to the receipt
- The log carries VNX_CONTRACT_OBSERVE_VIOLATION prefix
- The receipt status IS NOT changed (binding is a separate PR)
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

from envelope_govern import _CONTRACT_OBSERVE_MARKER, _govern
from envelope_types import EnvelopeSpec, _AdapterResult


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
        dispatch_id="test-contract-001",
        terminal_id="T1",
        provider="deepseek-harness",
        model="deepseek-v4-pro",
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_invalid_report(data_dir: Path, dispatch_id: str) -> Path:
    """Write a deliberately contract-invalid report — no mandatory headings."""
    reports_dir = data_dir / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{dispatch_id}.md"
    report_path.write_text(
        "# Dispatch test-contract-001\n\n"
        "This is the assembled prompt, not a proper report.\n"
        "It has none of the four mandatory report contract sections.\n",
        encoding="utf-8",
    )
    return report_path


def _write_valid_report(data_dir: Path, dispatch_id: str) -> Path:
    """Write a contract-compliant report with all four mandatory headings."""
    reports_dir = data_dir / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{dispatch_id}.md"
    report_path.write_text(
        "## Summary\n\n"
        + ("x" * 60)
        + "\n\n## Changes\n\n- Implemented feature X\n\n"
        "## Verification\n\n- pytest passed: 5/5\n\n"
        "## Open Items\n\nNone\n",
        encoding="utf-8",
    )
    return report_path


def _run_govern_with_mocks(spec, result, *, report_path_override=None):
    """Run _govern() with the minimum mocks needed for a test environment.

    emit_unified_report and emit_dispatch_receipt are mocked so we can
    control the report content and capture the receipt kwargs. Internal
    dependencies that touch the filesystem or external state are mocked
    to no-op or return safe defaults.
    """
    if report_path_override is not None:
        report_path = report_path_override
    else:
        report_path = spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md"

    # _govern() has a fail-closed guard: receipt_path must exist on disk after
    # emit. Pre-create the receipt file so the mock path points to a real file.
    receipt_path = spec.state_dir / "t0_receipts.ndjson"
    receipt_path.write_text("")

    mock_emit_report = MagicMock(return_value=report_path)
    mock_emit_receipt = MagicMock(return_value=receipt_path)

    with patch(
        "envelope_govern._archive_dispatch_events", return_value=("", True)
    ), patch(
        "envelope_govern._clear_dispatch_events"
    ), patch(
        "envelope_govern._receipt_exists_for_dispatch", return_value=False
    ), patch(
        "governance_emit.emit_unified_report", mock_emit_report
    ), patch(
        "governance_emit.emit_dispatch_receipt", mock_emit_receipt
    ):
        _govern(
            spec,
            result,
            start_time=datetime(2026, 8, 8, 12, 0, 0),
            end_time=datetime(2026, 8, 8, 12, 1, 0),
        )

    return mock_emit_receipt


# ---------------------------------------------------------------------------
# Tests — observable contract validation
# ---------------------------------------------------------------------------


class TestEnvelopeGovernContractObserve:
    """OI-1017/OI-1048 L2: observable body-contract validation on envelope lanes."""

    def test_invalid_report_produces_warning_in_receipt(self, spec, success_result):
        """A report without the four mandatory headings must produce a
        warnings[] entry in the receipt — observable, not binding."""
        # Pre-create a deliberately invalid report at the path
        # emit_unified_report will be mocked to return.
        report_path = _write_invalid_report(spec.data_dir, spec.dispatch_id)

        mock_emit_receipt = _run_govern_with_mocks(
            spec, success_result, report_path_override=report_path,
        )

        # The receipt was emitted — verify it carries the contract warning.
        call_kwargs = mock_emit_receipt.call_args.kwargs
        assert "warnings" in call_kwargs, (
            "emit_dispatch_receipt must receive a warnings= parameter"
        )
        assert call_kwargs["warnings"] is not None, (
            "warnings must not be None when the report body is contract-invalid"
        )
        assert len(call_kwargs["warnings"]) > 0, (
            "warnings list must be non-empty when the report body is contract-invalid"
        )
        warning = call_kwargs["warnings"][0]
        assert warning["code"] == "report_contract_violated", (
            f"warning code must be 'report_contract_violated', got {warning.get('code')!r}"
        )
        assert warning["severity"] == "warn"
        assert _CONTRACT_OBSERVE_MARKER in warning["message"], (
            f"warning message must contain the greppable marker "
            f"{_CONTRACT_OBSERVE_MARKER!r}"
        )
        # Observable mode: receipt status is NOT changed.
        assert call_kwargs["status"] == "success", (
            "receipt status must remain 'success' in observable mode "
            "(binding is a separate PR)"
        )

    def test_valid_report_produces_no_warning(self, spec, success_result):
        """A contract-compliant report must produce NO warnings entry."""
        report_path = _write_valid_report(spec.data_dir, spec.dispatch_id)

        mock_emit_receipt = _run_govern_with_mocks(
            spec, success_result, report_path_override=report_path,
        )

        call_kwargs = mock_emit_receipt.call_args.kwargs
        assert call_kwargs.get("warnings") is None, (
            "warnings must be None when the report body is contract-valid"
        )

    def test_missing_report_file_is_non_fatal(self, spec, success_result):
        """When the report file cannot be read (OSError), _govern must not
        break — it proceeds without warnings, not without a receipt."""
        # Point to a non-existent directory so read_text() raises OSError.
        report_path = spec.data_dir / "nonexistent" / f"{spec.dispatch_id}.md"

        mock_emit_receipt = _run_govern_with_mocks(
            spec, success_result, report_path_override=report_path,
        )

        # Receipt must still be emitted (fail-open on read error).
        assert mock_emit_receipt.called, (
            "receipt must be emitted even when report cannot be read"
        )
        call_kwargs = mock_emit_receipt.call_args.kwargs
        assert call_kwargs.get("warnings") is None, (
            "warnings must be None when report file is unreadable"
        )
        # Status is unchanged.
        assert call_kwargs["status"] == "success"

    def test_failure_report_still_validates_contract(self, spec):
        """A failed dispatch with an invalid report must still produce
        a contract warning — the check runs regardless of status."""
        report_path = _write_invalid_report(spec.data_dir, spec.dispatch_id)

        failed_result = _AdapterResult(
            returncode=1,
            completion_text="",
            status="failure",
            error="something went wrong",
        )

        mock_emit_receipt = _run_govern_with_mocks(
            spec, failed_result, report_path_override=report_path,
        )

        call_kwargs = mock_emit_receipt.call_args.kwargs
        assert call_kwargs["warnings"] is not None, (
            "contract validation must run for failure statuses too"
        )
        warning = call_kwargs["warnings"][0]
        assert warning["code"] == "report_contract_violated"
        # Status remains "failure" — observable, not binding.
        assert call_kwargs["status"] == "failure"

    def test_warning_is_filterable_by_code(self, spec, success_result):
        """The warning code must be a stable, greppable string consumers
        can filter on without parsing the human-readable message."""
        report_path = _write_invalid_report(spec.data_dir, spec.dispatch_id)

        mock_emit_receipt = _run_govern_with_mocks(
            spec, success_result, report_path_override=report_path,
        )

        call_kwargs = mock_emit_receipt.call_args.kwargs
        warnings = call_kwargs["warnings"]
        codes = {w["code"] for w in warnings}
        assert "report_contract_violated" in codes, (
            "filterable warning code must be present"
        )

    def test_missing_all_four_headings_are_listed(self, spec, success_result):
        """A report missing all four mandatory sections must list every
        missing heading in the warning message."""
        report_path = _write_invalid_report(spec.data_dir, spec.dispatch_id)

        mock_emit_receipt = _run_govern_with_mocks(
            spec, success_result, report_path_override=report_path,
        )

        call_kwargs = mock_emit_receipt.call_args.kwargs
        message = call_kwargs["warnings"][0]["message"]
        for heading in ("## Summary", "## Changes", "## Verification", "## Open Items"):
            assert heading in message, (
                f"missing heading {heading!r} must appear in the violation message"
            )
