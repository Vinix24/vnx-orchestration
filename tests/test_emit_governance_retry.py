"""test_emit_governance_retry.py — Tests for _emit_governance retry-with-backoff logic.

Kimi audit finding: _emit_governance raised SystemExit(1) on transient receipt
write failures. This module verifies the fixed behaviour: retry up to
_EMIT_MAX_RETRIES times with exponential backoff, then raise to caller.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from provider_dispatch import _emit_governance, _EMIT_MAX_RETRIES, _EMIT_RETRY_DELAY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        dispatch_id="retry-test-001",
        terminal_id="T1",
        pr_id=None,
        instruction="run the test",
        model="claude-sonnet-4-6",
        provider="claude",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _dummy_result():
    """Minimal spawn result that _extract_token_usage handles without crashing."""
    r = MagicMock()
    r.token_usage = {"input": 10, "output": 5, "cache_hit": 0}
    r.completion_text = "done"
    return r


# ---------------------------------------------------------------------------
# Test 1: successful write on first attempt — no retry
# ---------------------------------------------------------------------------

def test_success_first_attempt_no_retry(tmp_path):
    """Happy path: both emit calls succeed immediately. sleep must not be called."""
    args = _make_args()
    result = _dummy_result()
    receipt_path = tmp_path / "state" / "t0_receipts.ndjson"
    report_path = tmp_path / "data" / "unified_reports" / "retry-test-001.md"

    with (
        patch("governance_emit.emit_dispatch_receipt", return_value=receipt_path) as mock_receipt,
        patch("governance_emit.emit_unified_report", return_value=report_path) as mock_report,
        patch("provider_dispatch.time.sleep") as mock_sleep,
    ):
        _emit_governance(args, "claude", "claude-sonnet-4-6", result, _now(), _now(), "success")

    mock_receipt.assert_called_once()
    mock_report.assert_called_once()
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: transient RuntimeError on receipt — retries and eventually succeeds
# ---------------------------------------------------------------------------

def test_transient_receipt_error_retries_and_succeeds(tmp_path):
    """Two consecutive RuntimeErrors followed by success → retried, sleep called twice."""
    args = _make_args(dispatch_id="retry-test-002")
    result = _dummy_result()
    receipt_path = tmp_path / "t0_receipts.ndjson"

    side_effects = [
        RuntimeError("transient rename collision"),
        RuntimeError("transient rename collision again"),
        receipt_path,
    ]

    with (
        patch("governance_emit.emit_dispatch_receipt", side_effect=side_effects) as mock_receipt,
        patch("governance_emit.emit_unified_report", return_value=tmp_path / "report.md"),
        patch("provider_dispatch.time.sleep") as mock_sleep,
    ):
        _emit_governance(args, "claude", "claude-sonnet-4-6", result, _now(), _now(), "success")

    assert mock_receipt.call_count == 3
    assert mock_sleep.call_count == 2
    mock_sleep.assert_any_call(_EMIT_RETRY_DELAY * 1)
    mock_sleep.assert_any_call(_EMIT_RETRY_DELAY * 2)


# ---------------------------------------------------------------------------
# Test 3: persistent RuntimeError on receipt — raises after max retries, NOT SystemExit
# ---------------------------------------------------------------------------

def test_persistent_receipt_error_raises_not_systemexit(tmp_path):
    """Persistent failure → RuntimeError raised to caller, never SystemExit."""
    args = _make_args(dispatch_id="retry-test-003")
    result = _dummy_result()

    with (
        patch(
            "governance_emit.emit_dispatch_receipt",
            side_effect=RuntimeError("persistent disk failure"),
        ),
        patch("governance_emit.emit_unified_report"),
        patch("provider_dispatch.time.sleep"),
    ):
        with pytest.raises(RuntimeError, match="persistent disk failure"):
            _emit_governance(args, "claude", "claude-sonnet-4-6", result, _now(), _now(), "success")


def test_persistent_receipt_error_does_not_raise_systemexit(tmp_path):
    """Confirm that SystemExit is specifically NOT raised on persistent failure."""
    args = _make_args(dispatch_id="retry-test-003b")
    result = _dummy_result()

    with (
        patch(
            "governance_emit.emit_dispatch_receipt",
            side_effect=RuntimeError("disk full"),
        ),
        patch("governance_emit.emit_unified_report"),
        patch("provider_dispatch.time.sleep"),
    ):
        try:
            _emit_governance(args, "claude", "claude-sonnet-4-6", result, _now(), _now(), "success")
        except SystemExit:
            pytest.fail("_emit_governance raised SystemExit — should raise RuntimeError to caller")
        except RuntimeError:
            pass  # expected


def test_persistent_receipt_error_retries_exactly_max_times(tmp_path):
    """Exactly _EMIT_MAX_RETRIES calls are made before the final raise."""
    args = _make_args(dispatch_id="retry-test-003c")
    result = _dummy_result()
    call_count = 0

    def always_fail(*_a, **_kw):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("always fails")

    with (
        patch("governance_emit.emit_dispatch_receipt", side_effect=always_fail),
        patch("governance_emit.emit_unified_report"),
        patch("provider_dispatch.time.sleep"),
    ):
        with pytest.raises(RuntimeError):
            _emit_governance(args, "claude", "claude-sonnet-4-6", result, _now(), _now(), "success")

    assert call_count == _EMIT_MAX_RETRIES


# ---------------------------------------------------------------------------
# Test 4: FileExistsError (rename collision) is retried
# ---------------------------------------------------------------------------

def test_file_exists_error_wrapped_as_runtimeerror_retries(tmp_path):
    """FileExistsError from underlying fs → governance_emit wraps as RuntimeError → retried."""
    args = _make_args(dispatch_id="retry-test-004")
    result = _dummy_result()
    receipt_path = tmp_path / "t0_receipts.ndjson"

    # governance_emit wraps OSError (incl. FileExistsError) as RuntimeError
    wrapped = RuntimeError("governance_emit: receipt write failed: [Errno 17] File exists")

    side_effects = [wrapped, receipt_path]

    with (
        patch("governance_emit.emit_dispatch_receipt", side_effect=side_effects) as mock_receipt,
        patch("governance_emit.emit_unified_report", return_value=tmp_path / "report.md"),
        patch("provider_dispatch.time.sleep") as mock_sleep,
    ):
        _emit_governance(args, "claude", "claude-sonnet-4-6", result, _now(), _now(), "success")

    assert mock_receipt.call_count == 2
    mock_sleep.assert_called_once_with(_EMIT_RETRY_DELAY * 1)


# ---------------------------------------------------------------------------
# Test 5: ValueError (invalid provider) is NOT retried
# ---------------------------------------------------------------------------

def test_valueerror_not_retried(tmp_path):
    """ValueError from invalid provider → immediate raise without sleep/retry."""
    args = _make_args(dispatch_id="retry-test-005")
    result = _dummy_result()

    with (
        patch(
            "governance_emit.emit_dispatch_receipt",
            side_effect=ValueError("Invalid provider 'bad-provider'"),
        ) as mock_receipt,
        patch("governance_emit.emit_unified_report"),
        patch("provider_dispatch.time.sleep") as mock_sleep,
    ):
        with pytest.raises(ValueError, match="Invalid provider"):
            _emit_governance(args, "bad-provider", "model", result, _now(), _now(), "success")

    assert mock_receipt.call_count == 1
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
