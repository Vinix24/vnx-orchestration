#!/usr/bin/env python3
"""Tests for contract_invalid_ledger.py and its build_t0_state.py wiring (OI-1638).

envelope_govern.py already stamps ``status: "contract_invalid"`` on a receipt
when a worker's report fails the report-body contract — that write side is
proven (350+ live receipts). Before this module nothing turned that fact
into a visible count or a non-silent-closure gate. This file covers:

  - build_contract_invalid_summary: counts + per-provider breakdown, windowed.
  - collect_contract_invalid_open / write_contract_invalid_open_ledger: the
    per-dispatch open/closed ledger (a later governed-success receipt for the
    same dispatch_id resolves it).
  - is_deliverable_acceptable: the non-silent-closure gate.
  - build_t0_state.py wiring: the ``contract_invalid`` key in the full state
    dict and its compact form in t0_index.json.

Discipline: temp-DB/temp-ledger ONLY, mirrors tests/test_human_gate_queue.py's
isolation pattern (VNX_DATA_DIR_EXPLICIT=1 + tmp VNX_DATA_DIR, central-store
fallback blocked).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"

for _p in (str(_SCRIPTS_DIR), str(_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import contract_invalid_ledger as cil  # noqa: E402
import build_t0_state as bts  # noqa: E402

_PROJECT_ID = "vnx-dev"


def _pin_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin VNX_DATA_DIR_EXPLICIT=1 + a tmp VNX_DATA_DIR; return the state dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.setattr(bts, "resolve_central_data_dir", None, raising=False)
    return state_dir


def _write_receipts(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _ci_receipt(dispatch_id: str, provider: str, timestamp: str, **overrides) -> dict:
    rec = {
        "dispatch_id": dispatch_id,
        "provider": provider,
        "status": "contract_invalid",
        "event_type": "task_complete",
        "timestamp": timestamp,
        "report_path": f"/reports/{dispatch_id}.md",
    }
    rec.update(overrides)
    return rec


def _success_receipt(dispatch_id: str, provider: str, timestamp: str, **overrides) -> dict:
    rec = {
        "dispatch_id": dispatch_id,
        "provider": provider,
        "status": "success",
        "event_type": "task_complete",
        "timestamp": timestamp,
        "report_path": f"/reports/{dispatch_id}.md",
    }
    rec.update(overrides)
    return rec


# ---------------------------------------------------------------------------
# build_contract_invalid_summary
# ---------------------------------------------------------------------------

def test_summary_counts_two_of_three_and_by_provider(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
    _write_receipts(receipts, [
        _ci_receipt("d-1", "kimi", "2026-09-06T10:00:00Z"),
        _ci_receipt("d-2", "codex", "2026-09-06T11:00:00Z"),
        _success_receipt("d-3", "claude", "2026-09-06T11:30:00Z"),
    ])

    summary = cil.build_contract_invalid_summary(receipts, now=now)

    assert summary["total"] == 2
    assert summary["by_provider"] == {"kimi": 1, "codex": 1}


def test_summary_last_24h_window_excludes_older_records(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
    _write_receipts(receipts, [
        _ci_receipt("d-old", "kimi", "2026-09-01T00:00:00Z"),  # 5 days old
        _ci_receipt("d-recent", "kimi", "2026-09-06T09:00:00Z"),  # 3 hours old
    ])

    summary = cil.build_contract_invalid_summary(receipts, now=now)

    assert summary["total"] == 2
    assert summary["last_24h"] == 1
    assert summary["by_provider_last_24h"] == {"kimi": 1}


def test_summary_empty_ledger_is_zero_no_error(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"  # never created

    summary = cil.build_contract_invalid_summary(receipts)

    assert summary["total"] == 0
    assert summary["last_24h"] == 0
    assert summary["by_provider"] == {}


def test_summary_detects_via_report_contract_invalid_event_type(tmp_path: Path) -> None:
    """Older-schema receipts (2026-06 batch) carry event_type
    'report_contract_invalid' with no distinguishing status literal issue —
    both the status literal AND the event_type marker must be recognized.
    """
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        {
            "dispatch_id": "legacy-1",
            "provider": "unknown",
            "status": "contract_invalid",
            "event_type": "report_contract_invalid",
            "timestamp": "2026-06-03T08:51:09Z",
            "report_path": "/reports/legacy-1.md",
        },
    ])

    summary = cil.build_contract_invalid_summary(receipts)
    assert summary["total"] == 1


# ---------------------------------------------------------------------------
# collect_contract_invalid_open / write_contract_invalid_open_ledger
# ---------------------------------------------------------------------------

def test_open_lists_dispatch_with_no_later_success(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-open", "kimi", "2026-09-06T10:00:00Z"),
    ])

    open_items = cil.collect_contract_invalid_open(receipts)

    assert len(open_items) == 1
    item = open_items[0]
    assert item["dispatch_id"] == "d-open"
    assert item["provider"] == "kimi"
    assert item["report_path"] == "/reports/d-open.md"
    assert item["resolved"] is False


def test_open_excludes_dispatch_healed_by_later_success(tmp_path: Path) -> None:
    """A contract_invalid attempt followed by a later success receipt for
    the SAME dispatch_id is resolved and must not appear as open."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-healed", "kimi", "2026-09-06T10:00:00Z"),
        _success_receipt("d-healed", "kimi", "2026-09-06T10:05:00Z"),
    ])

    open_items = cil.collect_contract_invalid_open(receipts)

    assert open_items == []


def test_open_reopens_if_latest_receipt_regresses_to_contract_invalid(tmp_path: Path) -> None:
    """Chronological order matters, not append order: the LATEST receipt by
    effective timestamp decides open/closed, even if it was written earlier
    in the file (out-of-order ingestion)."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _success_receipt("d-regressed", "kimi", "2026-09-06T09:00:00Z"),
        _ci_receipt("d-regressed", "kimi", "2026-09-06T10:00:00Z"),
    ])

    open_items = cil.collect_contract_invalid_open(receipts)

    assert len(open_items) == 1
    assert open_items[0]["dispatch_id"] == "d-regressed"


def test_write_ledger_is_atomic_and_readable(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-1", "kimi", "2026-09-06T10:00:00Z"),
        _ci_receipt("d-2", "codex", "2026-09-06T10:05:00Z"),
    ])
    output_path = tmp_path / "state" / cil.OPEN_LEDGER_FILENAME

    payload = cil.write_contract_invalid_open_ledger(receipts, output_path)

    assert output_path.exists()
    assert not output_path.with_suffix(output_path.suffix + ".tmp").exists()
    on_disk = json.loads(output_path.read_text(encoding="utf-8"))
    assert on_disk["open_count"] == 2
    assert payload["open_count"] == 2
    assert {i["dispatch_id"] for i in on_disk["items"]} == {"d-1", "d-2"}


# ---------------------------------------------------------------------------
# is_deliverable_acceptable
# ---------------------------------------------------------------------------

def test_is_deliverable_acceptable_false_for_open_case(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-open", "kimi", "2026-09-06T10:00:00Z"),
    ])

    ok, reason = cil.is_deliverable_acceptable("d-open", receipts)

    assert ok is False
    assert "contract_invalid" in reason


def test_is_deliverable_acceptable_true_for_healed_case(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [
        _ci_receipt("d-healed", "kimi", "2026-09-06T10:00:00Z"),
        _success_receipt("d-healed", "kimi", "2026-09-06T10:05:00Z"),
    ])

    ok, reason = cil.is_deliverable_acceptable("d-healed", receipts)

    assert ok is True
    assert "not contract_invalid" in reason


def test_is_deliverable_acceptable_true_for_absent_dispatch(tmp_path: Path) -> None:
    """No receipt at all is absence, not a rejection (OI-1624 precedent) —
    this function's contract is narrower than "did this dispatch run"."""
    receipts = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts, [_ci_receipt("other-dispatch", "kimi", "2026-09-06T10:00:00Z")])

    ok, reason = cil.is_deliverable_acceptable("never-ran", receipts)

    assert ok is True
    assert "no receipt" in reason


def test_is_deliverable_acceptable_false_for_empty_dispatch_id(tmp_path: Path) -> None:
    receipts = tmp_path / "t0_receipts.ndjson"
    ok, reason = cil.is_deliverable_acceptable("", receipts)
    assert ok is False


# ---------------------------------------------------------------------------
# build_t0_state.py wiring: full-state key + t0_index compact form
# ---------------------------------------------------------------------------

def test_build_t0_state_surfaces_contract_invalid_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _write_receipts(state_dir / "t0_receipts.ndjson", [
        _ci_receipt("d-1", "kimi", "2026-09-06T10:00:00Z"),
        _ci_receipt("d-2", "codex", "2026-09-06T11:00:00Z"),
        _success_receipt("d-3", "claude", "2026-09-06T11:30:00Z"),
    ])
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert "contract_invalid" in state
    ci = state["contract_invalid"]
    assert ci["total"] == 2
    assert ci["by_provider"] == {"kimi": 1, "codex": 1}


def test_build_t0_state_contract_invalid_empty_ledger_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert state["contract_invalid"]["total"] == 0
    assert state["contract_invalid"]["last_24h"] == 0


def test_build_t0_state_preserves_existing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Additive-only: contract_invalid must not disturb existing sections."""
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert "canonical_tracks" in state
    assert "human_gate_queue" in state
    assert "contract_invalid" in state


def test_t0_index_carries_compact_contract_invalid_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()
    _write_receipts(state_dir / "t0_receipts.ndjson", [
        _ci_receipt("d-1", "kimi", "2026-09-06T10:00:00Z"),
    ])

    state = bts.build_t0_state(state_dir, dispatch_dir)
    index = bts._build_t0_index(state)

    assert "contract_invalid" in index
    assert index["contract_invalid"]["total"] == 1
    assert len(index) <= 50
    assert len(json.dumps(index)) < 5 * 1024
