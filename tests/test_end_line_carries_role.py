"""audit-spoor-kern K-fix-1: ``dispatch_finished`` carries ``role`` on every path.

Dispatch-ID: dlv-4903d3c17af3

Goedpunt 1 of ``audit-spoor-kern``: the start line and the end line both carry
``project_id``, ``dispatch_id``, ``role``, ``writer_sha`` and
``schema_version: 3``. K-meet measured 0/20 end lines with ``role``: the source
was the ``Handover``, which had no ``role``, and the sweep read the role from a
start line it never copied. This file pins the three write paths:

1. a lane exit: ``finalize_exit`` publishes the hand-over's role;
2. a sweep of an expired attempt: ``abandoned`` carries the start line's role;
3. crash recovery: an intent frozen without ``role`` is published unchanged.

Plus the enforcement: ``build_end_line`` has no default for ``role``, so a
writer that omits it fails with ``TypeError`` instead of dropping the field.

Every store is a real ledger and state dir under ``tmp_path``; nothing here
touches the real ledger.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import dispatch_exit  # noqa: E402
import dispatch_lifecycle as lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from append_receipt_internals.lifecycle import (  # noqa: E402
    build_end_intent,
    end_intent_path,
    load_end_intent,
    prepare_end_line,
    write_end_intent,
)
from append_receipt_internals.payload import append_receipt_payload  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402

PROJECT = "vnx-dev"
DISPATCH = "d-kfix1"
ATTEMPT = 1
ROLE = "backend-developer"
BASE_SHA = "a" * 40
HEAD_SHA = "c" * 40
START = "2026-11-01T10:00:00Z"
START_DT = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
DEADLINE = 300  # dispatch_spec.DEADLINE_SECONDS_MIN
GRACE = 30 * 60  # ADR-045 §6's grace, pinned literally
DOOR_START = "Mon Nov  3 10:00:00 2026"


@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    """Keep the real ledger layer, never fire its process-spawning hooks."""
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr("governance_receipts.append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "state"
    sd.mkdir()
    return sd


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "dispatch-spec.json"
    atomic_write_json(path, {"dispatch_id": DISPATCH, "deadline_seconds": DEADLINE})
    return path


@pytest.fixture()
def lifecycle_on(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _lines(state_dir: Path) -> list:
    ledger = _ledger(state_dir)
    if not ledger.exists():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _finished(state_dir: Path, dispatch_id: str = DISPATCH, attempt: int = ATTEMPT) -> list:
    return [line for line in _lines(state_dir)
            if line.get("event_type") == "dispatch_finished"
            and line.get("dispatch_id") == dispatch_id and line.get("attempt") == attempt]


def _append(state_dir: Path, line: dict):
    return append_receipt_payload(dict(line), receipts_file=str(_ledger(state_dir)),
                                  skip_enrichment=True)


def _start_line(**overrides) -> dict:
    line = {
        "timestamp": START, "event_type": "dispatch_started",
        "project_id": PROJECT, "dispatch_id": DISPATCH, "attempt": ATTEMPT,
        "role": ROLE, "base_sha": BASE_SHA, "spec_sha256": "0" * 64,
        "deadline_seconds": DEADLINE, "lifecycle_mode": "v3", "door_pid": 0,
        "door_started_at": "Mon Jan  1 00:00:00 2024", "requested_by": "terminal:T1",
    }
    line.update(overrides)
    return line


def _freeze_without_role(state_dir: Path, line: dict) -> Path:
    """Freeze an intent exactly as the pre-K-fix-1 code froze it: no ``role``."""
    ledger = _ledger(state_dir)
    prepared = prepare_end_line(line, ledger_path=ledger)
    intent_path = end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT)
    offset = ledger.stat().st_size if ledger.exists() else 0
    document = build_end_intent(
        project_id=PROJECT, dispatch_id=DISPATCH, attempt=ATTEMPT,
        end_state=str(prepared["end_state"]), end_line=prepared,
        ledger_path=ledger, ledger_offset=offset)
    assert write_end_intent(intent_path, document) is True
    return intent_path


# ---------------------------------------------------------------------------
# 1. the lane exit
# ---------------------------------------------------------------------------

def test_a_lane_exit_publishes_the_handover_role(state_dir, spec_file, lifecycle_on):
    with patch.object(dispatch_start, "process_started_at", lambda pid: DOOR_START), \
            patch.object(dispatch_start, "_now", lambda: START):
        handover = dispatch_start.begin(
            project_id=PROJECT, dispatch_id=DISPATCH, role=ROLE, base_sha=BASE_SHA,
            spec_file=spec_file, target_slot="T1", state_dir=state_dir)

    assert handover.role == ROLE
    result = dispatch_exit.finalize_exit(handover, state_dir=state_dir, status="success")

    assert result is not None and result.status == lifecycle.STATUS_FINALIZED
    finished = _finished(state_dir)
    assert len(finished) == 1, finished
    assert finished[0]["role"] == ROLE


# ---------------------------------------------------------------------------
# 2. the sweep of an expired attempt
# ---------------------------------------------------------------------------

def test_a_sweep_abandoned_line_carries_the_start_lines_role(state_dir, lifecycle_on):
    _append(state_dir, _start_line())
    now = START_DT + timedelta(seconds=DEADLINE + GRACE + 60)

    result = lifecycle.sweep(state_dir=state_dir, now=now)

    assert result.abandoned == 1, result
    finished = _finished(state_dir)
    assert len(finished) == 1, finished
    assert finished[0]["end_state"] == "abandoned"
    assert finished[0]["role"] == ROLE


# ---------------------------------------------------------------------------
# 3. crash recovery of an intent frozen before the role
# ---------------------------------------------------------------------------

def test_recover_publishes_a_roleless_intent_unchanged(state_dir, lifecycle_on):
    line = {"timestamp": START, "event_type": "dispatch_finished", "project_id": PROJECT,
            "dispatch_id": DISPATCH, "attempt": ATTEMPT, "end_state": "completed",
            "head_sha": HEAD_SHA, "model": "deepseek-flash"}
    intent_path = _freeze_without_role(state_dir, line)
    frozen = json.loads(load_end_intent(intent_path)["line"])
    assert "role" not in frozen

    result = lifecycle.recover(state_dir=state_dir, project_id=PROJECT,
                               dispatch_id=DISPATCH, attempt=ATTEMPT)

    assert result.status == lifecycle.STATUS_RECOVERED
    finished = _finished(state_dir)
    assert len(finished) == 1, finished
    assert "role" not in finished[0], finished[0]
    assert finished[0]["end_state"] == "completed"
    assert "role" not in json.loads(load_end_intent(intent_path)["line"])


# ---------------------------------------------------------------------------
# 4. the enforcement: no default for role
# ---------------------------------------------------------------------------

def test_build_end_line_without_role_is_a_type_error():
    handover = dispatch_start.Handover(
        project_id=PROJECT, dispatch_id=DISPATCH, attempt=ATTEMPT, lifecycle_mode="v3",
        door_pid=0, door_started_at="", started_at=START, role=ROLE)

    with pytest.raises(TypeError):
        dispatch_exit.build_end_line(handover, end_state="completed", head_sha=HEAD_SHA)
