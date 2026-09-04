"""test_dispatch_refire_guard.py — golf 1A, points 1 + 2
(dispatch-20260904-deur-bezit-dispatch-toestand).

Point 1 (hervuur-wachter): the door refuses a dispatch_id that already
carries a terminal receipt, a route decision, or a runtime end-state, unless
the caller passes an explicit --refire reason.

Point 2 (gesloten cutover-wachtrij): the guard's evidence queue
(t0_receipts.ndjson, route_decisions.ndjson, runtime_coordination.db) is read
EXCLUSIVELY, keyed by dispatch_id — dispatches/pending/ (the historical
bundle-spec directory, 2100+ entries on the live store) is NEVER scanned.

Every test in this file is RED on the branch point (no refire guard existed
at all — a second real fire of the same dispatch_id always proceeded) and
GREEN on this fix. Tests run against a throwaway data_dir under tmp_path,
never the live central store.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_cli import run_dispatch


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_bundle(tmp_path: Path, *, staging_id: str, dispatch_id: str) -> "tuple[Path, Path]":
    """A promoted-style staged bundle (spec + instruction inside the bundle dir)."""
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(instruction),
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": "codex_gate",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
        "force_tmux": True,
        "force_tmux_reason": "refire-guard test asserts door decisions, not lane behavior",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _write_terminal_receipt(state_dir: Path, dispatch_id: str, *, status: str = "success") -> None:
    """Compact NDJSON (no space after ':'), matching the real ledger's shape
    — the guard's substring pre-filter looks for '"dispatch_id":"<id>"'."""
    state_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": 2,
        "dispatch_id": dispatch_id,
        "status": status,
        "timestamp": "2026-09-01T00:00:00Z",
    }
    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(receipt, separators=(",", ":")) + "\n")


def _write_route_decision(state_dir: Path, dispatch_id: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": "2026-09-01T00:00:00Z", "dispatch_id": dispatch_id, "decision": {}}
    with (state_dir / "route_decisions.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def _write_runtime_end_state(state_dir: Path, dispatch_id: str, state: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dispatches (
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued'
        )
        """
    )
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, 'vnx-dev', ?)",
        (dispatch_id, state),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Point 1 — a terminal receipt blocks a re-fire.
# ---------------------------------------------------------------------------

def test_terminal_receipt_blocks_refire_without_reason(tmp_path, monkeypatch, capsys):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-refire-receipt", dispatch_id="20260904-refire-receipt",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    _write_terminal_receipt(data_dir / "state", "20260904-refire-receipt", status="success")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "a dispatch_id with an existing terminal receipt must be refused"
    mock_execute.assert_not_called()
    err = capsys.readouterr().err
    assert "refire-blocked" in err
    assert "--refire" in err


def test_terminal_receipt_refire_with_reason_proceeds(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-refire-receipt-ok", dispatch_id="20260904-refire-receipt-ok",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    _write_terminal_receipt(data_dir / "state", "20260904-refire-receipt-ok", status="failure")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file, refire_reason="operator: known-good re-run after fixing the lane")

    assert rc == 0, "an explicit --refire reason must let the dispatch proceed"
    mock_execute.assert_called_once()


# ---------------------------------------------------------------------------
# Point 1 — a route decision (no receipt yet) also blocks a re-fire.
# ---------------------------------------------------------------------------

def test_route_decision_blocks_refire_without_reason(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-refire-route", dispatch_id="20260904-refire-route",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    _write_route_decision(data_dir / "state", "20260904-refire-route")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "a dispatch_id with an existing route decision must be refused"
    mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# Point 1 — a runtime end-state (completed/failed_delivery/...) blocks a
# re-fire; a NON-end-state (e.g. 'proposed', the pre-cutover legacy shape)
# does NOT.
# ---------------------------------------------------------------------------

def test_runtime_end_state_blocks_refire_without_reason(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-refire-endstate", dispatch_id="20260904-refire-endstate",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    _write_runtime_end_state(data_dir / "state", "20260904-refire-endstate", "failed_delivery")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "a dispatch_id already at a runtime end-state must be refused"
    mock_execute.assert_not_called()


def test_non_end_state_row_does_not_block_refire(tmp_path, monkeypatch):
    """Point 2: a pre-cutover row sitting at a NON-end-state (e.g. the old
    ad-hoc 'proposed') must not read as prior evidence — the guard only
    reacts to _REFIRE_RUNTIME_END_STATES, not to row EXISTENCE."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-refire-proposed", dispatch_id="20260904-refire-proposed",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    _write_runtime_end_state(data_dir / "state", "20260904-refire-proposed", "proposed")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0, "a row parked at a non-end-state must not block a first real fire"
    mock_execute.assert_called_once()


# ---------------------------------------------------------------------------
# Point 1 — no prior evidence at all -> clear to fire.
# ---------------------------------------------------------------------------

def test_no_prior_evidence_fires_clean(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-refire-clean", dispatch_id="20260904-refire-clean",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_execute.assert_called_once()


# ---------------------------------------------------------------------------
# Point 2 — the guard NEVER scans dispatches/pending/, even when a matching
# dispatch_id directory sits right there with real spec content. Proven by
# planting a huge, live-shaped pending/ population (including the exact
# dispatch_id under test) and asserting Path.iterdir/os.scandir/os.listdir
# are never invoked on that directory while the guard runs.
# ---------------------------------------------------------------------------

def test_refire_guard_never_scans_pending_directory(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-refire-noscan", dispatch_id="20260904-refire-noscan",
    )
    pending_dir = data_dir / "dispatches" / "pending"
    # A large, realistic-shaped historical population, INCLUDING a directory
    # literally named after the dispatch_id under test — if the guard ever
    # scanned pending/ instead of reading the ledger/DB by key, this would be
    # the most tempting false signal to trip on.
    for i in range(50):
        (pending_dir / f"20260706-historical-{i}").mkdir(parents=True, exist_ok=True)
    (pending_dir / "20260904-refire-noscan").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    real_iterdir = Path.iterdir
    real_scandir_names = []

    def _guarded_iterdir(self):
        if self == pending_dir or pending_dir in self.parents:
            real_scandir_names.append(str(self))
        return real_iterdir(self)

    with patch("dispatch_cli._execute_claude", return_value=0), \
         patch.object(Path, "iterdir", _guarded_iterdir):
        rc = run_dispatch(spec_file)

    assert rc == 0
    assert real_scandir_names == [], (
        "the refire guard (and dispatch acceptance) must never iterate "
        f"dispatches/pending/ — but it did: {real_scandir_names}"
    )
