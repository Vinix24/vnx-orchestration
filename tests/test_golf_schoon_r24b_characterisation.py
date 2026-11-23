#!/usr/bin/env python3
"""Characterisation of ``reconcile_terminal_state`` before and after R24b.

golf schoon R24b cuts the per-terminal loop body and the repair document out
of ``reconcile_terminal_state``. These cases pin its observable behaviour on
the old code: the whole result document, the bytes of the repaired file, the
untouched file in read-only mode, the probe call order and a probe error
that propagates before any write.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

LIB_DIR = Path(__file__).resolve().parents[1] / "scripts" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "_r24b_terminal_state_reconciler", LIB_DIR / "terminal_state_reconciler.py"
)
tsr = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tsr
_SPEC.loader.exec_module(tsr)

NOW = datetime(2026, 2, 10, 12, 0, 30, tzinfo=timezone.utc)
NOW_ISO = "2026-02-10T12:00:30+00:00"


def _tmux(calls=None):
    def probe(terminals, state_dir, allow):
        if calls is not None:
            calls.append(("tmux", tuple(terminals), Path(state_dir), allow))
        return {
            "T1": {"pane_id": "%1", "pane_alive": True, "current_command": "claude",
                   "recent_log_activity": "2026-02-10T12:00:05+00:00",
                   "seconds_since_log_activity": 10},
            "T2": {"pane_id": "%2", "pane_alive": False, "current_command": None,
                   "recent_log_activity": None, "seconds_since_log_activity": None},
        }
    return probe


def _proc(healthy=True, calls=None):
    def probe():
        if calls is not None:
            calls.append(("proc",))
        return {"processes": {"dispatcher": healthy, "receipt_processor": healthy,
                              "heartbeat_monitor": False},
                "core_pipeline_healthy": healthy}
    return probe


def _receipts(path: Path, rows: list) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _primary_doc() -> dict:
    return {
        "schema_version": 1,
        "terminals": {
            "T1": {"terminal_id": "T1", "status": "claimed", "claimed_by": "d-1",
                   "claimed_at": "2026-02-10T12:00:00+00:00",
                   "lease_expires_at": "2026-02-10T12:05:00+00:00",
                   "last_activity": "2026-02-10T12:00:01+00:00", "version": 4},
            "T2": {"terminal_id": "T2", "status": "working", "claimed_by": "d-2",
                   "claimed_at": "2026-02-10T11:00:00+00:00",
                   "lease_expires_at": "2026-02-10T11:05:00+00:00",
                   "last_activity": "2026-02-10T11:00:01+00:00", "version": 2},
            "T3": {"terminal_id": "T3", "status": "idle", "claimed_by": None,
                   "claimed_at": None, "lease_expires_at": None,
                   "last_activity": "2026-02-10T09:00:00+00:00", "version": 7},
        },
    }


def _strip_clock(result: dict) -> dict:
    health = result["primary_health"]
    assert (health["mtime"] is None) == (health["age_seconds"] is None)
    health["mtime"] = "<mtime>" if health["mtime"] is not None else None
    health["age_seconds"] = "<age>" if health["age_seconds"] is not None else None
    return result


def _expected_records(t1_source: str, t2_source: str, t3_source: str) -> dict:
    return {
        "T1": {"terminal_id": "T1", "status": "active", "claimed_by": "d-1",
               "claimed_at": "2026-02-10T12:00:00+00:00",
               "lease_expires_at": "2026-02-10T12:05:00+00:00",
               "last_activity": "2026-02-10T12:00:05+00:00", "version": 4,
               "reconciled_source": t1_source},
        "T2": {"terminal_id": "T2", "status": "unknown", "claimed_by": None,
               "claimed_at": None, "lease_expires_at": None,
               "last_activity": "2026-02-10T11:00:01+00:00", "version": 2,
               "reconciled_source": t2_source},
        "T3": {"terminal_id": "T3", "status": "idle", "claimed_by": None,
               "claimed_at": None, "lease_expires_at": None,
               "last_activity": "2026-02-10T12:00:20+00:00", "version": 7,
               "reconciled_source": t3_source},
    }


def _t3_complete(tmp_path: Path) -> Path:
    path = tmp_path / "t0_receipts.ndjson"
    _receipts(path, [
        {"event_type": "task_started", "terminal": "T3",
         "timestamp": "2026-02-10T12:00:10+00:00", "dispatch_id": "d-3"},
        {"event_type": "report_ingested", "terminal": "T3",
         "timestamp": "2026-02-10T12:00:25+00:00", "dispatch_id": "d-3"},
        {"event_type": "task_complete", "terminal": "T3",
         "timestamp": "2026-02-10T12:00:20+00:00", "dispatch_id": "d-3"},
        "not-a-dict",
    ])
    return path


def test_stale_primary_in_repair_mode_writes_the_file(tmp_path: Path):
    primary_path = tmp_path / "terminal_state.json"
    primary_path.write_text(json.dumps(_primary_doc()), encoding="utf-8")
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()
    os.utime(primary_path, (stale, stale))
    receipts_path = _t3_complete(tmp_path)

    result = tsr.reconcile_terminal_state(
        tmp_path, config=tsr.ReconcilerConfig(stale_after_seconds=5), repair=True,
        now=NOW, tmux_probe=_tmux(), process_probe=_proc(),
    )

    records = _expected_records("fallback", "fallback", "fallback")
    assert _strip_clock(result) == {
        "schema_version": 1, "generated_at": NOW_ISO, "mode": "repair",
        "degraded": True, "degraded_reasons": ["primary_stale"],
        "primary_health": {"exists": True, "valid": True, "stale": True,
                           "error": "primary_stale", "mtime": "<mtime>", "age_seconds": "<age>"},
        "process_health": _proc()(), "terminals": records,
        "evidence": {"receipts_scanned": 3, "receipt_file": str(receipts_path), "tmux": _tmux()(None, None, None)},
        "repair_written": str(primary_path),
    }
    expected_payload = {"schema_version": 1, "terminals": {
        term: {k: v for k, v in rec.items() if k != "reconciled_source"}
        for term, rec in records.items()}}
    assert primary_path.read_text(encoding="utf-8") == json.dumps(
        expected_payload, indent=2, sort_keys=True) + "\n"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_fresh_primary_read_only_leaves_the_file_untouched(tmp_path: Path):
    primary_path = tmp_path / "terminal_state.json"
    raw = json.dumps(_primary_doc())
    primary_path.write_text(raw, encoding="utf-8")
    mtime_before = primary_path.stat().st_mtime_ns
    _t3_complete(tmp_path)
    calls: list = []

    result = tsr.reconcile_terminal_state(
        tmp_path, now=NOW, tmux_probe=_tmux(calls), process_probe=_proc(False, calls),
    )

    assert calls == [("tmux", ("T1", "T2", "T3"), tmp_path, True), ("proc",)]
    assert primary_path.read_text(encoding="utf-8") == raw
    assert primary_path.stat().st_mtime_ns == mtime_before
    assert "repair_written" not in result
    assert result["mode"] == "read_only"
    assert result["degraded_reasons"] == ["process_health_degraded"]
    assert result["primary_health"]["error"] is None
    assert result["primary_health"]["stale"] is False
    records = _expected_records("hybrid", "hybrid", "hybrid")
    records["T3"]["reconciled_source"] = "hybrid"
    # T2 pane is dead, but the pipeline is unhealthy, so the base status stays.
    records["T2"]["status"] = "working"
    assert result["terminals"] == records
    assert list(result) == ["schema_version", "generated_at", "mode", "degraded",
                            "degraded_reasons", "primary_health", "process_health",
                            "terminals", "evidence"]


def test_fresh_primary_without_evidence_is_primary_source(tmp_path: Path):
    doc = _primary_doc()
    (tmp_path / "terminal_state.json").write_text(json.dumps(doc), encoding="utf-8")

    result = tsr.reconcile_terminal_state(
        tmp_path, now=NOW, tmux_probe=lambda *_: {}, process_probe=_proc(),
    )

    assert [r["reconciled_source"] for r in result["terminals"].values()] == ["primary"] * 3
    assert result["degraded"] is False
    assert result["evidence"]["receipts_scanned"] == 0
    assert result["terminals"]["T3"]["last_activity"] == "2026-02-10T09:00:00+00:00"


def test_missing_primary_with_split_brain(tmp_path: Path):
    _receipts(tmp_path / "t0_receipts.ndjson", [
        {"event_type": "task_started", "terminal": "T1", "dispatch_id": "dup"},
        {"status": "no_confirmation", "terminal": "T2", "task_id": "dup",
         "timestamp": "2026-02-10T12:00:00+00:00"},
    ])

    result = tsr.reconcile_terminal_state(
        tmp_path, now=NOW, tmux_probe=lambda *_: {}, process_probe=_proc(),
    )

    assert result["degraded_reasons"] == ["primary_missing", "split_brain_conflicting_claim:dup:T1,T2"]
    assert result["primary_health"] == {"exists": False, "valid": False, "stale": False,
                                        "error": "primary_missing", "mtime": None, "age_seconds": None}
    assert result["terminals"] == {
        "T1": {"terminal_id": "T1", "status": "working", "claimed_by": "dup", "claimed_at": None,
               "lease_expires_at": None, "last_activity": NOW_ISO, "version": 1,
               "reconciled_source": "fallback"},
        "T2": {"terminal_id": "T2", "status": "blocked", "claimed_by": "dup", "claimed_at": None,
               "lease_expires_at": None, "last_activity": "2026-02-10T12:00:00+00:00", "version": 1,
               "reconciled_source": "fallback"},
        "T3": {"terminal_id": "T3", "status": "idle", "claimed_by": None, "claimed_at": None,
               "lease_expires_at": None, "last_activity": NOW_ISO, "version": 1,
               "reconciled_source": "fallback"},
    }
    assert not (tmp_path / "terminal_state.json").exists()


def test_corrupt_primary_repair_replaces_it(tmp_path: Path):
    primary_path = tmp_path / "terminal_state.json"
    primary_path.write_text("{not json", encoding="utf-8")

    result = tsr.reconcile_terminal_state(
        tmp_path, repair=True, now=NOW, tmux_probe=lambda *_: {}, process_probe=_proc(),
    )

    assert result["degraded_reasons"] == ["primary_corrupt:JSONDecodeError"]
    assert result["repair_written"] == str(primary_path)
    written = json.loads(primary_path.read_text(encoding="utf-8"))
    assert sorted(written["terminals"]["T1"]) == ["claimed_at", "claimed_by", "last_activity",
                                                  "lease_expires_at", "status", "terminal_id", "version"]


def test_probe_error_propagates_before_any_write(tmp_path: Path):
    primary_path = tmp_path / "terminal_state.json"
    raw = json.dumps(_primary_doc())
    primary_path.write_text(raw, encoding="utf-8")

    def broken_probe():
        raise RuntimeError("probe down")

    with pytest.raises(RuntimeError, match="probe down"):
        tsr.reconcile_terminal_state(
            tmp_path, repair=True, now=NOW, tmux_probe=lambda *_: {}, process_probe=broken_probe,
        )
    assert primary_path.read_text(encoding="utf-8") == raw
