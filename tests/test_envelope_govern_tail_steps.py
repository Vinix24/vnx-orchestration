"""test_envelope_govern_tail_steps.py — characterisation of the two closing
steps of ``envelope_govern._govern`` (golf 2, G2-6a).

``_govern`` ends with (A) one status line — error / contract-override warning /
info — and (B) the inline phantom guard with its two fail-open backstops, then
returns ``(report_path, receipt_path)``. G2-6a moves A and B into helpers
without changing behaviour, so this file pins the observable behaviour of both
steps on the UNCHANGED code first and is kept green across the move.

Step A is observed through ``caplog`` (level + message + arguments of the single
line). Step B is observed through the guard's two entry points — patched where
they are looked up (``phantom_guard.record_phantom_if_any`` /
``phantom_guard.record_guard_error``, both imported lazily inside ``_govern``)
and through ``envelope_govern._publish_phantom_verdict``, the module-level name
``_govern`` calls.

The fixtures and the mocked emission seam are the ones
``tests/test_envelope_govern_report_adoption.py`` already uses; the report
emitter stays REAL so the status line reflects a report actually on disk.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import envelope_govern  # noqa: E402
import phantom_guard  # noqa: E402
from envelope_govern import _govern  # noqa: E402
from envelope_types import EnvelopeSpec, _AdapterResult  # noqa: E402

_LOGGER_NAME = "envelope_govern"


# ---------------------------------------------------------------------------
# Fixtures — the adoption file's fixtures, plus token usage for the guard args
# ---------------------------------------------------------------------------


@pytest.fixture()
def spec(tmp_path):
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    data_dir.mkdir()
    state_dir.mkdir()
    return EnvelopeSpec(
        dispatch_id="test-envelope-tail-001",
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
        token_usage={"input": 120, "output": 30},
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
    """Run ``_govern`` with the same emission/receipt seam the adoption tests mock.

    ``emit_unified_report`` stays real so ``report_path`` is the file the status
    line names; ``emit_dispatch_receipt`` is mocked so the receipt half never
    touches external state.
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
        report_path, receipt_path = _govern(
            spec,
            result,
            start_time=datetime(2026, 11, 6, 12, 0, 0),
            end_time=datetime(2026, 11, 6, 12, 1, 0),
        )

    return report_path, receipt_path


def _status_lines(caplog):
    """The single closing status line, picked out of every captured record."""
    return [
        record for record in caplog.records
        if record.name == _LOGGER_NAME
        and record.getMessage().startswith("envelope._govern: dispatch=")
    ]


# ---------------------------------------------------------------------------
# Step A — the closing status log
# ---------------------------------------------------------------------------


class TestClosingStatusLog:

    def test_failed_result_logs_the_error_line_with_completion_len_and_error(
        self, spec, consumer_root, caplog,
    ):
        result = _AdapterResult(
            returncode=1,
            completion_text="partial output",
            status="failure",
            error="provider crashed",
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            report_path, receipt_path = _run_govern(spec, result, consumer_root)

        lines = _status_lines(caplog)
        assert len(lines) == 1, [r.getMessage() for r in lines]
        record = lines[0]
        assert record.levelno == logging.ERROR
        message = record.getMessage()
        assert spec.dispatch_id in message
        assert "provider=claude" in message
        assert "status=failure" in message
        assert f"report={report_path}" in message
        assert f"receipt={receipt_path}" in message
        assert "completion_len=14" in message
        assert "error=provider crashed" in message

    def test_failed_result_without_error_text_logs_no_error_captured(
        self, spec, consumer_root, caplog,
    ):
        result = _AdapterResult(
            returncode=1,
            completion_text="",
            status="timeout",
            error=None,
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            _run_govern(spec, result, consumer_root)

        lines = _status_lines(caplog)
        assert len(lines) == 1, [r.getMessage() for r in lines]
        assert lines[0].levelno == logging.ERROR
        message = lines[0].getMessage()
        assert "completion_len=0" in message
        assert "error=(no error captured)" in message

    def test_contract_override_logs_the_warning(
        self, spec, success_result, consumer_root, caplog,
    ):
        # The generic ## Response wrapper fails the body contract, so a claimed
        # success is downgraded to contract_invalid before the closing line.
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            _run_govern(spec, success_result, consumer_root)

        lines = _status_lines(caplog)
        assert len(lines) == 1, [r.getMessage() for r in lines]
        record = lines[0]
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert spec.dispatch_id in message
        assert "adapter=success" in message
        assert "effective=contract_invalid" in message
        assert "(contract override)" in message

    def test_clean_success_logs_the_info_line(
        self, spec, success_result, consumer_root, caplog,
    ):
        # A contract-valid report on disk keeps the adapter's own success, so
        # the third branch (info) is the one that runs.
        central = spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md"
        central.parent.mkdir(parents=True, exist_ok=True)
        central.write_text(_valid_body(), encoding="utf-8")

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            report_path, receipt_path = _run_govern(spec, success_result, consumer_root)

        lines = _status_lines(caplog)
        assert len(lines) == 1, [r.getMessage() for r in lines]
        record = lines[0]
        assert record.levelno == logging.INFO
        message = record.getMessage()
        assert spec.dispatch_id in message
        assert "status=success" in message
        assert f"report={report_path}" in message
        assert f"receipt={receipt_path}" in message


# ---------------------------------------------------------------------------
# Step B — the inline phantom guard
# ---------------------------------------------------------------------------


class TestInlinePhantomGuard:

    def test_verdict_is_published_with_the_govern_arguments(
        self, spec, success_result, consumer_root, monkeypatch,
    ):
        verdict = phantom_guard.PhantomVerdict(True, "PHANTOM: nothing delivered")
        captured = {}

        def _record(**kwargs):
            captured.update(kwargs)
            return verdict

        monkeypatch.setattr(phantom_guard, "record_phantom_if_any", _record)
        publish = MagicMock()
        monkeypatch.setattr(envelope_govern, "_publish_phantom_verdict", publish)

        _run_govern(spec, success_result, consumer_root)

        publish.assert_called_once_with(verdict, success_result)
        assert captured["dispatch_id"] == spec.dispatch_id
        # the token sum of token_usage, not the dict itself
        assert captured["token_usage"] == 150
        assert captured["receipts_file"] == str(spec.state_dir / "t0_receipts.ndjson")
        assert captured["state_dir"] == spec.state_dir
        assert captured["provider"] == "claude"
        assert captured["terminal"] == "T1"
        assert captured["worktree_path"] is None
        assert captured["base_sha"] is None
        assert captured["context"].role == "backend-developer"
        assert captured["context"].status == "success"
        assert captured["context"].read_only is None

    def test_token_sum_is_none_when_no_tokens_were_spent(
        self, spec, consumer_root, monkeypatch,
    ):
        result = _AdapterResult(
            returncode=0, completion_text="done", status="success",
            token_usage={"input": 0, "output": 0},
        )
        captured = {}

        def _record(**kwargs):
            captured.update(kwargs)
            return phantom_guard.PhantomVerdict(False, "ok")

        monkeypatch.setattr(phantom_guard, "record_phantom_if_any", _record)
        monkeypatch.setattr(envelope_govern, "_publish_phantom_verdict", MagicMock())

        _run_govern(spec, result, consumer_root)

        assert captured["token_usage"] is None

    def test_guard_failure_logs_one_error_and_records_the_guard_error(
        self, spec, success_result, consumer_root, monkeypatch, caplog,
    ):
        def _boom(**kwargs):
            raise RuntimeError("guard exploded")

        guard_errors = []
        monkeypatch.setattr(phantom_guard, "record_phantom_if_any", _boom)
        monkeypatch.setattr(
            phantom_guard, "record_guard_error",
            lambda **kwargs: guard_errors.append(kwargs),
        )

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            report_path, receipt_path = _run_govern(spec, success_result, consumer_root)

        assert report_path is not None
        assert receipt_path == spec.state_dir / "t0_receipts.ndjson"
        failures = [
            record for record in caplog.records
            if "phantom-guard check failed (non-fatal)" in record.getMessage()
        ]
        assert len(failures) == 1, [r.getMessage() for r in caplog.records]
        assert failures[0].levelno == logging.ERROR
        assert spec.dispatch_id in failures[0].getMessage()
        assert "guard exploded" in failures[0].getMessage()

        assert len(guard_errors) == 1
        assert guard_errors[0]["dispatch_id"] == spec.dispatch_id
        assert guard_errors[0]["receipts_file"] == str(spec.state_dir / "t0_receipts.ndjson")
        assert isinstance(guard_errors[0]["error"], RuntimeError)
        assert str(guard_errors[0]["error"]) == "guard exploded"

    def test_guard_error_audit_failure_is_non_fatal(
        self, spec, success_result, consumer_root, monkeypatch, caplog,
    ):
        def _boom(**kwargs):
            raise RuntimeError("guard exploded")

        def _boom_too(**kwargs):
            raise RuntimeError("audit exploded")

        monkeypatch.setattr(phantom_guard, "record_phantom_if_any", _boom)
        monkeypatch.setattr(phantom_guard, "record_guard_error", _boom_too)

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            report_path, receipt_path = _run_govern(spec, success_result, consumer_root)

        # _govern still returns its paths — the second failure never escapes.
        assert report_path == spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md"
        assert receipt_path == spec.state_dir / "t0_receipts.ndjson"
        audit_failures = [
            record for record in caplog.records
            if "guard-error audit signal itself failed" in record.getMessage()
        ]
        assert len(audit_failures) == 1, [r.getMessage() for r in caplog.records]
        assert audit_failures[0].levelno == logging.ERROR
        assert spec.dispatch_id in audit_failures[0].getMessage()
