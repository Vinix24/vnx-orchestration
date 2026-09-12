"""tests/test_envelope_govern_spawn_error.py — OI-1716: the envelope path must
thread the spawn-layer diagnosis (adapter_result.error) into the unified report,
so an empty completion never falls back to "(no response captured)" when the
spawn layer already knows why it produced nothing.

The provider lane already does this (#1834, provider_dispatch._emit_governance
passes ``spawn_error=getattr(result, "error", None)``); envelope_govern._govern
is a second entrance to the same emitter and inherited nothing from it.

The test asserts on the WRITTEN report's behavior, not on the source: with an
empty completion_text and a filled error, the report body must carry the
spawn-error diagnosis (the error text, under the emitter's spawn-error label)
instead of the contentless placeholder. RED against the pre-fix envelope_govern
(the error text never reaches the report; the body falls back to
"(no response captured)").
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

_SPAWN_ERROR = "litellm proxy unreachable at http://127.0.0.1:1 (start the proxy first)"


@pytest.fixture()
def spec(tmp_path):
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    data_dir.mkdir()
    state_dir.mkdir()
    return EnvelopeSpec(
        dispatch_id="oi1716-spawn-error-001",
        terminal_id="T1",
        provider="glm-harness",
        model="glm-5.2",
        instruction="do the thing",
        role="backend-developer",
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
    )


def _run_govern_with_real_report_emit(spec, result):
    """Run _govern() letting the REAL emit_unified_report write the report.

    emit_dispatch_receipt and the filesystem/event dependencies are mocked so
    the receipt half does not touch external state — but the report half runs
    the real emitter, so the written .md is the observable artifact the test
    asserts on.
    """
    receipt_path = spec.state_dir / "t0_receipts.ndjson"
    receipt_path.write_text("")

    mock_emit_receipt = MagicMock(return_value=receipt_path)

    with patch(
        "envelope_govern._archive_dispatch_events", return_value=("", True)
    ), patch(
        "envelope_govern._clear_dispatch_events"
    ), patch(
        "envelope_govern._receipt_exists_for_dispatch", return_value=False
    ), patch(
        "governance_emit.emit_dispatch_receipt", mock_emit_receipt
    ):
        _govern(
            spec,
            result,
            start_time=datetime(2026, 9, 12, 12, 0, 0),
            end_time=datetime(2026, 9, 12, 12, 1, 0),
        )

    return mock_emit_receipt


def test_empty_completion_with_error_surfaces_spawn_error_in_report(spec):
    """An empty completion_text plus a filled adapter error must put the
    spawn-layer diagnosis into the written report, not "(no response captured)".

    Forces the state actively: the adapter result is constructed with an empty
    completion and a concrete error. The report written to disk must carry that
    error under the emitter's spawn-error label (the distinctive "spawn error"
    phrasing, not the lane-log lift) and must not fall back to the placeholder.
    """
    result = _AdapterResult(
        returncode=1,
        completion_text="",
        status="failure",
        error=_SPAWN_ERROR,
    )

    _run_govern_with_real_report_emit(spec, result)

    report_path = spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md"
    content = report_path.read_text(encoding="utf-8")

    assert _SPAWN_ERROR in content, (
        "the written report must carry the spawn-layer diagnosis verbatim, "
        f"not fall back to '(no response captured)': {content!r}"
    )
    assert "(no response captured)" not in content, (
        "an empty response with a known spawn error must never degrade to the "
        "contentless placeholder"
    )
    assert "spawn error" in content, (
        "the diagnosis must be labeled as a spawn error (not a model reply, "
        "not a lane-log lift) so readers never misread it as model output"
    )
