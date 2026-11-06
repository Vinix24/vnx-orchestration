"""test_envelope_govern_report_steps.py — characterisation of the three middle
steps of ``envelope_govern._govern`` (golf 2, G2-6b).

``_govern`` runs, between the event archive and the receipt write:

  A. the stray-report adoption — resolve a worker-authored report found in the
     worktree/outer checkout, validate it and relocate it into the central
     store (``report_relocated``);
  B. the report emit — ``emit_unified_report`` with its keyword arguments,
     failure logged and non-fatal;
  C. the body-contract check — ``validate_body`` on the report, the
     ``report_contract_violated`` warning, the OI-2519 ``governed_status``
     override and the OSError skip.

G2-6b moves A, B and C into private helpers without changing behaviour, so
this file pins the observable behaviour of all three on the UNCHANGED code
first and is kept green across the move.

Fixtures and the mocked emission seam are the ones
``tests/test_envelope_govern_report_adoption.py``,
``tests/test_envelope_govern_contract_enforce.py`` and
``tests/test_envelope_govern_tail_steps.py`` already use: the event archive,
the dedup probe, the consumer-root resolver and ``emit_dispatch_receipt`` are
patched; ``emit_unified_report`` stays REAL in the adoption cases (the
relocated file is only observable through the real emitter's idempotent
early-return) and is mocked in the cases that need to control its failure or
its return path.
"""
from __future__ import annotations

import logging
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from envelope_govern import _govern  # noqa: E402
from envelope_types import EnvelopeSpec, _AdapterResult  # noqa: E402

_LOGGER_NAME = "envelope_govern"
_UNSET = object()


# ---------------------------------------------------------------------------
# Fixtures — the adoption/contract/tail fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def spec(tmp_path):
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    data_dir.mkdir()
    state_dir.mkdir()
    return EnvelopeSpec(
        dispatch_id="test-envelope-steps-001",
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


def _placeholder_body() -> str:
    """All four sections present, but the Summary is the injected placeholder."""
    return (
        "## Summary\n\n"
        "Interactive tmux dispatch (lane: tmux_interactive). Status: written.\n\n"
        "## Changes\n\n- none\n\n"
        "## Verification\n\n- none\n\n"
        "## Open Items\n\nNone\n"
    )


def _write_invalid_report(data_dir: Path, dispatch_id: str) -> Path:
    reports_dir = data_dir / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{dispatch_id}.md"
    report_path.write_text(
        "# Dispatch report\n\n"
        "This is the assembled prompt, not a proper report.\n"
        "It has none of the four mandatory report contract sections.\n",
        encoding="utf-8",
    )
    return report_path


def _run_govern(spec, result, consumer_root, *, emit_report=_UNSET, extra_patches=()):
    """Run ``_govern`` with the seam every characterisation file patches."""
    receipts_file = spec.state_dir / "t0_receipts.ndjson"
    receipts_file.write_text("")

    mock_emit_receipt = MagicMock(return_value=receipts_file)

    with ExitStack() as stack:
        stack.enter_context(
            patch("envelope_govern._archive_dispatch_events", return_value=("", True))
        )
        stack.enter_context(patch("envelope_govern._clear_dispatch_events"))
        stack.enter_context(
            patch("envelope_govern._receipt_exists_for_dispatch", return_value=False)
        )
        stack.enter_context(
            patch(
                "dispatch_worktree_isolation.resolve_consumer_project_root",
                return_value=consumer_root,
            )
        )
        stack.enter_context(
            patch("governance_emit.emit_dispatch_receipt", mock_emit_receipt)
        )
        if emit_report is not _UNSET:
            stack.enter_context(
                patch("governance_emit.emit_unified_report", emit_report)
            )
        for extra in extra_patches:
            stack.enter_context(extra)

        report_path, receipt_path = _govern(
            spec,
            result,
            start_time=datetime(2026, 11, 6, 12, 0, 0),
            end_time=datetime(2026, 11, 6, 12, 1, 0),
        )

    return report_path, receipt_path, mock_emit_receipt


def _logs_matching(caplog, needle: str):
    return [record for record in caplog.records if needle in record.getMessage()]


# ---------------------------------------------------------------------------
# Step A — the stray-report adoption
# ---------------------------------------------------------------------------


class TestStrayReportAdoption:

    def test_valid_stray_report_in_outer_checkout_is_adopted(
        self, spec, success_result, consumer_root, caplog,
    ):
        stray_dir = consumer_root / ".vnx-data" / "unified_reports"
        stray_dir.mkdir(parents=True)
        stray_path = stray_dir / f"{spec.dispatch_id}.md"
        stray_path.write_text(_valid_body(), encoding="utf-8")

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            report_path, receipt_path, mock_emit_receipt = _run_govern(
                spec, success_result, consumer_root,
            )

        central = spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md"
        assert report_path == central
        assert receipt_path == spec.state_dir / "t0_receipts.ndjson"
        assert central.exists()
        assert "Implemented the feature correctly" in central.read_text(encoding="utf-8")
        assert not stray_path.exists(), "the stray original must be relocated"

        call_kwargs = mock_emit_receipt.call_args.kwargs
        warnings = call_kwargs["warnings"]
        # report_relocated comes FIRST in the warnings list, and the adopted
        # report passes the contract, so it is the only entry.
        assert [w["code"] for w in warnings] == ["report_relocated"]
        assert str(stray_path) in warnings[0]["message"]
        assert call_kwargs["status"] == "success"

        adoption_logs = _logs_matching(caplog, "adopted stray worker report")
        assert len(adoption_logs) == 1, [r.getMessage() for r in caplog.records]
        assert adoption_logs[0].levelno == logging.INFO
        message = adoption_logs[0].getMessage()
        assert spec.dispatch_id in message
        assert str(stray_path) in message
        assert str(central) in message

    def test_placeholder_stray_report_is_not_adopted(
        self, spec, success_result, consumer_root, caplog,
    ):
        stray_dir = consumer_root / ".vnx-data" / "unified_reports"
        stray_dir.mkdir(parents=True)
        stray_path = stray_dir / f"{spec.dispatch_id}.md"
        stray_path.write_text(_placeholder_body(), encoding="utf-8")

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            report_path, _receipt_path, mock_emit_receipt = _run_govern(
                spec, success_result, consumer_root,
            )

        # Left in place, and the generic wrapper was synthesized over it.
        assert stray_path.exists()
        assert "Interactive tmux dispatch" in stray_path.read_text(encoding="utf-8")
        assert "## Response" in report_path.read_text(encoding="utf-8")
        warnings = mock_emit_receipt.call_args.kwargs["warnings"]
        assert not any(w["code"] == "report_relocated" for w in warnings)
        assert not _logs_matching(caplog, "adopted stray worker report")

    def test_adoption_resolver_raising_logs_one_warning_and_continues(
        self, spec, success_result, consumer_root, caplog,
    ):
        boom = patch(
            "dispatch_govern._resolve_worker_report_with_adoption",
            side_effect=RuntimeError("resolver exploded"),
        )

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            report_path, receipt_path, mock_emit_receipt = _run_govern(
                spec, success_result, consumer_root, extra_patches=(boom,),
            )

        # Governance continued: the wrapper was emitted and the receipt written.
        assert report_path == spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md"
        assert report_path.exists()
        assert receipt_path == spec.state_dir / "t0_receipts.ndjson"
        assert mock_emit_receipt.called

        failure_logs = _logs_matching(caplog, "stray-report adoption failed")
        assert len(failure_logs) == 1, [r.getMessage() for r in caplog.records]
        assert failure_logs[0].levelno == logging.WARNING
        message = failure_logs[0].getMessage()
        assert spec.dispatch_id in message
        assert "resolver exploded" in message


# ---------------------------------------------------------------------------
# Step B — the report emit
# ---------------------------------------------------------------------------


class TestReportEmit:

    def test_emit_raising_logs_one_error_and_the_receipt_is_still_attempted(
        self, spec, success_result, consumer_root, caplog,
    ):
        mock_emit_report = MagicMock(side_effect=RuntimeError("emit exploded"))

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            report_path, receipt_path, mock_emit_receipt = _run_govern(
                spec, success_result, consumer_root, emit_report=mock_emit_report,
            )

        assert report_path is None
        assert receipt_path == spec.state_dir / "t0_receipts.ndjson"
        assert mock_emit_receipt.called, "the receipt must still be attempted"
        receipt_kwargs = mock_emit_receipt.call_args.kwargs
        assert receipt_kwargs["report_path"] is None
        assert receipt_kwargs["warnings"] is None

        error_logs = _logs_matching(caplog, "report emit failed")
        assert len(error_logs) == 1, [r.getMessage() for r in caplog.records]
        assert error_logs[0].levelno == logging.ERROR
        message = error_logs[0].getMessage()
        assert spec.dispatch_id in message
        assert "emit exploded" in message
        assert "proceeding to receipt" in message


# ---------------------------------------------------------------------------
# Step C — the body-contract check
# ---------------------------------------------------------------------------


class TestBodyContractCheck:

    def test_invalid_report_on_success_warns_and_publishes_contract_invalid(
        self, spec, consumer_root, caplog,
    ):
        result = _AdapterResult(
            returncode=0,
            completion_text="Worker output without contract headings.",
            status="success",
        )
        report_path_on_disk = _write_invalid_report(spec.data_dir, spec.dispatch_id)
        mock_emit_report = MagicMock(return_value=report_path_on_disk)

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            report_path, receipt_path, mock_emit_receipt = _run_govern(
                spec, result, consumer_root, emit_report=mock_emit_report,
            )

        assert report_path == report_path_on_disk
        assert receipt_path == spec.state_dir / "t0_receipts.ndjson"
        receipt_kwargs = mock_emit_receipt.call_args.kwargs
        assert [w["code"] for w in receipt_kwargs["warnings"]] == [
            "report_contract_violated"
        ]
        assert receipt_kwargs["status"] == "contract_invalid"
        # OI-2519: status is never rewritten; the governed verdict is separate.
        assert result.status == "success"
        assert result.governed_status == "contract_invalid"

        violation_logs = _logs_matching(caplog, "VNX_CONTRACT_ENFORCE_VIOLATION")
        assert len(violation_logs) == 1, [r.getMessage() for r in caplog.records]
        assert violation_logs[0].levelno == logging.WARNING
        assert "## Summary" in violation_logs[0].getMessage()

    def test_invalid_report_on_failure_warns_without_touching_governed_status(
        self, spec, consumer_root, caplog,
    ):
        result = _AdapterResult(
            returncode=1,
            completion_text="",
            status="failure",
            error="something went wrong",
        )
        report_path_on_disk = _write_invalid_report(spec.data_dir, spec.dispatch_id)
        mock_emit_report = MagicMock(return_value=report_path_on_disk)

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            report_path, _receipt_path, mock_emit_receipt = _run_govern(
                spec, result, consumer_root, emit_report=mock_emit_report,
            )

        assert report_path == report_path_on_disk
        receipt_kwargs = mock_emit_receipt.call_args.kwargs
        assert [w["code"] for w in receipt_kwargs["warnings"]] == [
            "report_contract_violated"
        ]
        # The original failure takes precedence, and the governed field stays
        # untouched (None) — the override only fires on an adapter success.
        assert receipt_kwargs["status"] == "failure"
        assert result.governed_status is None
        assert len(_logs_matching(caplog, "VNX_CONTRACT_ENFORCE_VIOLATION")) == 1

    def test_unreadable_report_skips_the_contract_check_without_a_warning(
        self, spec, success_result, consumer_root, caplog,
    ):
        missing = spec.data_dir / "nonexistent" / f"{spec.dispatch_id}.md"
        mock_emit_report = MagicMock(return_value=missing)

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            report_path, receipt_path, mock_emit_receipt = _run_govern(
                spec, success_result, consumer_root, emit_report=mock_emit_report,
            )

        assert report_path == missing
        assert receipt_path == spec.state_dir / "t0_receipts.ndjson"
        receipt_kwargs = mock_emit_receipt.call_args.kwargs
        assert receipt_kwargs["warnings"] is None
        assert receipt_kwargs["status"] == "success"
        assert not _logs_matching(caplog, "VNX_CONTRACT_ENFORCE_VIOLATION")
