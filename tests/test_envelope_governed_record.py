"""ADR-054 (audit-spoor-kern K-fix-3): the envelope's record of a v3 dispatch.

That record used to be a ``task_complete`` — a completion type — so every v3
dispatch had a second end line. ADR-054 keeps it but writes it as the
non-completion ``dispatch_governed``; a legacy dispatch keeps ``task_complete``.
Every case runs the REAL ``_govern`` and finalizer against a ``tmp_path`` ledger.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts"))

import completion_reading  # noqa: E402
import dispatch_exit  # noqa: E402
import dispatch_start  # noqa: E402
import ledger_end_line  # noqa: E402
import receipt_outcome  # noqa: E402
from dispatch_envelope import _LaneExit  # noqa: E402
from envelope_govern import _govern  # noqa: E402
from envelope_govern_support import _is_lane_completion_receipt  # noqa: E402
from envelope_types import EnvelopeSpec, _AdapterResult  # noqa: E402
from event_outcome_semantics import classify_event_outcome  # noqa: E402
from receipt_schema import DISPATCH_GOVERNED_EVENT_TYPE, ReceiptV2  # noqa: E402

DISPATCH_ID = "20261106-kfix3-v3"
PROJECT = "proj-a"
ROLE = "backend-developer"
START = datetime(2026, 11, 6, 12, 0, 0)
END = datetime(2026, 11, 6, 12, 1, 0)

# The 17 fields the K-meet measured as unique to the envelope's record (B.1).
UNIQUE_FIELDS = frozenset({
    "completion_pct", "cost_ledger_run_ids", "cost_source", "cost_usd",
    "duration_seconds", "events_path", "final_prompt_path", "findings",
    "injection_reconstructs", "risk", "role_applied", "role_source_path",
    "role_tier", "skill_applied", "skill_source_path", "terminal_id", "token_usage",
})

_VALID_BODY = (
    "## Summary\n\nImplemented the feature with full test coverage. All tests pass "
    "and the implementation is complete and correct in every regard.\n\n"
    "## Changes\n\n- Implemented feature X\n\n"
    "## Verification\n\n- pytest passed: 5/5\n\n"
    "## Open Items\n\nNone\n"
)


def _make_dir(root: Path) -> Path:
    for sub in ("state", "unified_reports"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def store(tmp_path, monkeypatch):
    data_dir = _make_dir(tmp_path / "proj-a")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    return data_dir


def _ledger(state_dir: Path) -> list:
    path = state_dir / "t0_receipts.ndjson"
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _spec(data_dir: Path) -> EnvelopeSpec:
    return EnvelopeSpec(DISPATCH_ID, "T1", "claude", "sonnet", "do the thing", ROLE, None,
                        data_dir / "state", data_dir)


def _handover(mode: str) -> dispatch_start.Handover:
    ts = "2026-11-06T00:00:00Z"
    return dispatch_start.Handover(PROJECT, DISPATCH_ID, 1, mode, 1, ts, ts)


def _run_govern(data_dir: Path, *, lifecycle_v3: bool):
    """Run the REAL GOVERN with every external seam the tests do not own stubbed."""
    (data_dir / "unified_reports" / f"{DISPATCH_ID}.md").write_text(
        _VALID_BODY, encoding="utf-8")
    # The event archive/clear helpers resolve VNX_DATA_DIR to tmp_path.
    with patch("envelope_govern._book_ledger_run",
               return_value=({"cost_usd": 0.5, "cost_source": "price_table@2026-09-14",
                              "run_id": "run-1"}, None)), \
         patch("envelope_govern._verify_role_application", return_value=SimpleNamespace(
             role_applied=True, tier="prompt_assembler", reason=None,
             source_path="/roles/backend-developer.md")), \
         patch("envelope_govern.skill_receipt_fields",
               return_value={"skill_applied": True, "skill_source_path": "/skills/x/SKILL.md"}):
        result = _AdapterResult(returncode=0, completion_text=_VALID_BODY, status="success",
                                token_usage={"input": 10, "output": 5})
        integrity = SimpleNamespace(final_prompt_path="/tmp/final_prompt.md",
                                    final_prompt_sha256="ab" * 32, injection_reconstructs=True)
        return _govern(_spec(data_dir), result, start_time=START, end_time=END,
                       integrity=integrity, lifecycle_v3=lifecycle_v3)


def _finalize_v3(store: Path) -> list:
    _run_govern(store, lifecycle_v3=True)
    dispatch_exit.finalize_exit(
        _handover(dispatch_start.MODE_V3), state_dir=store / "state", status="success")
    return _ledger(store / "state")


# 1. A v3 lane exit: one governed record, no second end line.

def test_v3_lane_exit_writes_one_governed_record_and_no_task_complete(store):
    lines = _finalize_v3(store)
    governed = [r for r in lines if r.get("event_type") == DISPATCH_GOVERNED_EVENT_TYPE]
    assert len(governed) == 1, governed
    assert UNIQUE_FIELDS <= set(governed[0]), sorted(UNIQUE_FIELDS - set(governed[0]))
    assert [r for r in lines if r.get("event_type") == "task_complete"] == []
    assert len([r for r in lines if r.get("event_type") == "dispatch_finished"]) == 1


def test_legacy_dispatch_still_writes_task_complete_with_the_same_field_set(store, tmp_path):
    v3_dir = _make_dir(tmp_path / "proj-v3")
    _run_govern(store, lifecycle_v3=False)
    _run_govern(v3_dir, lifecycle_v3=True)
    legacy = [r for r in _ledger(store / "state") if r.get("event_type") == "task_complete"]
    governed = [r for r in _ledger(v3_dir / "state")
                if r.get("event_type") == DISPATCH_GOVERNED_EVENT_TYPE]
    assert len(legacy) == 1 and len(governed) == 1
    # Same record, only the event type differs — the legacy shape is unchanged.
    assert set(legacy[0]) == set(governed[0])
    assert legacy[0]["schema_version"] == governed[0]["schema_version"] == 2
    assert legacy[0]["status"] == governed[0]["status"]


# 2. dispatch_governed is no outcome and never a witness of the end.

@pytest.mark.parametrize("status", ["success", "failure", "blocked", None, "", "made_up"])
def test_dispatch_governed_classifies_as_no_outcome(status):
    assert classify_event_outcome(DISPATCH_GOVERNED_EVENT_TYPE, status) is None


def test_dispatch_governed_is_no_witness_and_the_ledger_end_is_dispatch_finished(store):
    lines = _finalize_v3(store)
    governed = next(r for r in lines if r["event_type"] == DISPATCH_GOVERNED_EVENT_TYPE)
    assert completion_reading.is_completion(governed) is False
    assert receipt_outcome.is_outcome_line(governed) is False
    assert [r["event_type"] for r in completion_reading.dedupe_completions(lines)] == [
        "dispatch_finished"]
    end = ledger_end_line.end_line(lines, PROJECT, DISPATCH_ID)
    assert end is not None and end["event_type"] == "dispatch_finished"
    assert ledger_end_line.end_outcome(end) == "success"


# 3. ReceiptV2 carries exactly two event types.

def _receipt_kwargs(**extra):
    kwargs = dict(
        dispatch_id=DISPATCH_ID, terminal_id="T1", provider="claude", model="sonnet",
        status="success", completion_pct=100, risk=0.0, findings=[],
        duration_seconds=1.0, token_usage={"input": 1, "output": 1}, receipt_kind="dispatch")
    kwargs.update(extra)
    return kwargs


@pytest.mark.parametrize(
    "event_type", ["forged", "dispatch_finished", "report_ingested", "", None])
def test_receipt_v2_raises_for_any_other_event_type(event_type):
    with pytest.raises(ValueError, match="event_type"):
        ReceiptV2(**_receipt_kwargs(event_type=event_type))


def test_receipt_v2_accepts_dispatch_governed_with_schema_version_two():
    record = ReceiptV2(**_receipt_kwargs(event_type=DISPATCH_GOVERNED_EVENT_TYPE)).to_dict()
    assert (record["event_type"], record["schema_version"]) == (DISPATCH_GOVERNED_EVENT_TYPE, 2)


# 4. The dedupe finds either type, so a re-run never writes a second record.

def test_dedupe_finds_an_existing_governed_record_and_writes_no_second(store):
    _run_govern(store, lifecycle_v3=True)
    governed = [r for r in _ledger(store / "state")
                if r["event_type"] == DISPATCH_GOVERNED_EVENT_TYPE][0]
    assert _is_lane_completion_receipt(governed, DISPATCH_ID) is True
    _run_govern(store, lifecycle_v3=True)
    assert len([r for r in _ledger(store / "state")
                if r["event_type"] == DISPATCH_GOVERNED_EVENT_TYPE]) == 1


# 5. The lane exit reads v3 off the Handover; no Handover stays legacy.

@pytest.mark.parametrize("mode,expected", [
    (dispatch_start.MODE_V3, True), (dispatch_start.MODE_LEGACY, False)])
def test_lane_exit_passes_lifecycle_v3_from_the_handover(store, mode, expected):
    seen = {}
    exit_ = _LaneExit(_handover(mode), state_dir=store / "state", wt_path=None,
                      dispatch_id=DISPATCH_ID, project_root=store, terminal_id="T1")
    def _capture(*args, **kwargs):
        seen.update(kwargs)
        return (None, None)

    with patch("dispatch_envelope._govern", side_effect=_capture), \
         patch.object(exit_, "close"):
        exit_.govern(_spec(store), _AdapterResult(returncode=0, completion_text="",
                                                  status="success"), START, END)
    assert seen["lifecycle_v3"] is expected
