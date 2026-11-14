"""tests/test_envelope_govern_transient_api_error.py — OI-2796 (golf schoon F3).

A builder that dies on a transient API error (the CLI's ``API Error: <code>``
line as the last thing it said, no report) keeps its status but gets a named
reason: ``adapter_result.error`` becomes ``transient_api_error: <the line>``
instead of staying empty, which logged ``(no error captured)``.
"""
from __future__ import annotations

import logging, sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from envelope_govern import _govern
from envelope_govern_support import transient_api_error_line
from envelope_types import EnvelopeSpec, _AdapterResult

# The real tail of dlv-b452a2bd798c's ## Response (13-11): sentences glued together.
_CLOUDFLARE_TAIL = (
    "De sed faalde, dus het subsysteem is niet gewijzigd. Ik pas het met Edit aan."
    "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare\n"
)
_VALID_REPORT = (
    "# Report\n\n## Summary\n\nThe builder finished the work and wrote this report body in full.\n\n"
    "## Changes\n\n- scripts/lib/x.py\n\n## Verification\n\npytest tests/x.py: 1 passed\n\n"
    "## Open Items\n\nNone\n"
)


@pytest.fixture()
def spec(tmp_path):
    data_dir, state_dir = tmp_path / "data", tmp_path / "state"
    data_dir.mkdir(), state_dir.mkdir()
    return EnvelopeSpec(
        dispatch_id="oi2796-transient-001",
        terminal_id="T1", provider="claude", model="opus", instruction="build the thing",
        role="backend-developer", pr_id=None, state_dir=state_dir, data_dir=data_dir,
    )


def _govern_with_real_report(spec, result):
    receipt_path = spec.state_dir / "t0_receipts.ndjson"
    receipt_path.write_text("")
    with patch("envelope_govern._archive_dispatch_events", return_value=("", True)), patch(
        "envelope_govern._clear_dispatch_events"
    ), patch("envelope_govern._receipt_exists_for_dispatch", return_value=False), patch(
        "governance_emit.emit_dispatch_receipt", MagicMock(return_value=receipt_path)
    ) as emit_receipt:
        _govern(
            spec, result,
            start_time=datetime(2026, 11, 13, 12, 0, 0),
            end_time=datetime(2026, 11, 13, 12, 5, 0),
        )
    return emit_receipt


def test_cloudflare_403_without_report_names_the_line(spec, caplog):
    result = _AdapterResult(returncode=1, completion_text=_CLOUDFLARE_TAIL, status="failure")
    with caplog.at_level(logging.ERROR, logger="envelope_govern"):
        emit_receipt = _govern_with_real_report(spec, result)
    expected = "transient_api_error: Failed to authenticate. API Error: 403 Attention Required! | Cloudflare"
    assert result.error == expected
    assert "(no error captured)" not in caplog.text
    assert expected in caplog.text
    # No new canonical status: the receipt keeps what the contract decided.
    assert emit_receipt.call_args.kwargs["status"] == "failure"


def test_success_claim_without_report_stays_contract_invalid(spec):
    result = _AdapterResult(returncode=0, completion_text=_CLOUDFLARE_TAIL, status="success")
    emit_receipt = _govern_with_real_report(spec, result)
    assert emit_receipt.call_args.kwargs["status"] == "contract_invalid"
    assert result.error.startswith("transient_api_error: ")


def test_valid_report_leaves_error_and_status_untouched(spec):
    reports = spec.data_dir / "unified_reports"
    reports.mkdir()
    (reports / f"{spec.dispatch_id}.md").write_text(_VALID_REPORT, encoding="utf-8")
    result = _AdapterResult(returncode=0, completion_text=_CLOUDFLARE_TAIL, status="success")
    emit_receipt = _govern_with_real_report(spec, result)
    assert result.error is None
    assert emit_receipt.call_args.kwargs["status"] == "success"


def test_filled_adapter_error_is_never_overwritten(spec):
    result = _AdapterResult(returncode=1, completion_text=_CLOUDFLARE_TAIL, status="failure",
                            error="claude exited 1: stream closed")
    _govern_with_real_report(spec, result)
    assert result.error == "claude exited 1: stream closed"


@pytest.mark.parametrize("text,expected", [
    ('API Error: 529 {"type":"overloaded_error"}', 'API Error: 529 {"type":"overloaded_error"}'),
    ("work done.\nAPI Error: 529 Overloaded\n", "API Error: 529 Overloaded"),
    ("API Error: 500 Internal server error", "API Error: 500 Internal server error"),
    ("API Error: 502 Bad Gateway", "API Error: 502 Bad Gateway"),
    ("API Error: 503 Service Unavailable", "API Error: 503 Service Unavailable"),
    (_CLOUDFLARE_TAIL, "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare"),
])
def test_recognised_lines(text, expected):
    assert transient_api_error_line(text) == expected


@pytest.mark.parametrize("text", [
    "The API errored with a 529 overloaded response, so I retried.",
    "Cloudflare showed Attention Required on a 403, odd.",
    "API Error: 403 Forbidden",           # a plain 403 is no Cloudflare edge error
    "API Error: 401 Invalid bearer token",
    "API Error: 402 Insufficient Balance",
    "API Error: 5000 nonsense",
    "",
    None,
])
def test_unrecognised_lines(text):
    assert transient_api_error_line(text) is None


def test_line_is_cut_at_200_and_carries_no_key():
    key = "sk-ant-oat01-" + "A" * 40
    text = f"API Error: 500 upstream said Bearer {key} " + "x" * 400
    line = transient_api_error_line(text)
    assert len(line) <= 200
    assert key not in line and "sk-ant" not in line
    assert line.startswith("API Error: 500 ")
