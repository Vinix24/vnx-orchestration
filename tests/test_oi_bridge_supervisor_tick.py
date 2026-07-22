"""tests/test_oi_bridge_supervisor_tick.py — D1 (oi-bridge-continuous PR-1).

Verifies the OI→track bridge is wired into the dispatcher SUPERVISOR tick
(scripts/lib/dispatcher_supervisor_ticks.sh: _maybe_oi_bridge_tick), NOT
RoadmapManager.autopilot_tick() (default-off, no continuous cadence):

  * flag-gated: VNX_SUPERVISOR_MODE!=unified is a no-op (bit-identical legacy).
  * a new OI with a valid pr_ref is bridged into track_open_items by one tick
    call; a second tick call makes no duplicate row (idempotent, R4.4).
  * a bridge failure (malformed/absent open_items.json) never crashes the
    supervisor (best-effort, survives `set -euo pipefail`) and writes the
    freshness signal ($STATE_DIR/.oi_bridge_fresh) to "0".
  * _maybe_objective_reconcile withholds --apply when the bridge freshness
    signal is not "1" (missing, or explicitly "0") — the safety-critical
    bridge-failure <-> reconcile-freshness coupling — and includes --apply
    once the signal reads "1".
  * process_dispatches() calls the bridge tick BEFORE the reconcile tick (the
    bridge must run first so the reconciler's derived_status / blocker-check
    reads freshly-synced track_open_items in the same tick).

Real bridge script + real (temp) SQLite DB — no mocking of
import_open_items_to_tracks.py itself (out of scope for D1: only the caller
changed). The reconcile leg uses a stub `python3` on PATH to capture the
built command line without running the (already independently tested)
objective_reconcile machinery.
"""
from __future__ import annotations

import os
import re
import sqlite3
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKS_SH = REPO_ROOT / "scripts" / "lib" / "dispatcher_supervisor_ticks.sh"
DISPATCHER_SH = REPO_ROOT / "scripts" / "dispatcher_minimal.sh"
MIGRATIONS = REPO_ROOT / "schemas" / "migrations"

_LIB = REPO_ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import schema_migration  # noqa: E402
import tracks as tracks_lib  # noqa: E402

PROJECT_ID = "test-oi-bridge-tick"


# ---------------------------------------------------------------------------
# DB fixture (mirrors tests/test_oi_track_bridge.py _build_db_v30)
# ---------------------------------------------------------------------------

def _build_db_v30(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT,
            gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT, actor TEXT, reason TEXT, metadata_json TEXT,
            occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            project_id TEXT
        )
    """)
    conn.commit()
    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
    ]:
        sql = (MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.close()


def _link_rows(state_dir: Path, oi_id: str) -> list:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT track_id, link_type, resolved_at FROM track_open_items "
        "WHERE project_id = ? AND oi_id = ? ORDER BY track_id",
        (PROJECT_ID, oi_id),
    )]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Bash harness: source the real ticks file, call the real functions
# ---------------------------------------------------------------------------

def _base_env(state_dir: Path, tmp_path: Path) -> dict:
    env = dict(os.environ)
    env["VNX_SUPERVISOR_MODE"] = "unified"
    env["STATE_DIR"] = str(state_dir)
    env["VNX_LOGS_DIR"] = str(tmp_path / "logs")
    env["VNX_DIR"] = str(REPO_ROOT)
    env["VNX_DATA_DIR"] = str(state_dir.parent)
    env["VNX_PROJECT_ID"] = PROJECT_ID
    return env


def _run_bash(body: str, env: dict) -> subprocess.CompletedProcess:
    """Source the real dispatcher_supervisor_ticks.sh and run `body` under
    `set -euo pipefail` — a bridge failure that trips an unguarded bare
    command would abort here before SURVIVED prints (best-effort regression)."""
    script = textwrap.dedent(f"""\
        set -euo pipefail
        log() {{ printf '[log] %s\\n' "$*" >> "$VNX_LOG_CAPTURE"; }}
        source "{TICKS_SH}"
        {body}
        echo "SURVIVED:$?"
        """)
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30,
    )


def _seed_blocking_oi(state_dir: Path, *, oi_id: str = "OI-1", pr_ref: str = "#100") -> None:
    tracks_lib.create_track(
        state_dir, "T-bridge", PROJECT_ID, "T-bridge", "goal",
        phase="active", pr_ref=pr_ref,
    )
    import json
    (state_dir / "open_items.json").write_text(
        json.dumps({"items": [
            {"id": oi_id, "severity": "blocker", "status": "open",
             "title": "blocking item", "pr_id": pr_ref},
        ]}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Gating: legacy mode is a no-op
# ---------------------------------------------------------------------------

def test_bridge_tick_noop_in_legacy_mode(tmp_path):
    """VNX_SUPERVISOR_MODE unset/legacy: the bridge never runs, no state/fresh files."""
    state_dir = tmp_path / "state"
    _build_db_v30(state_dir)
    _seed_blocking_oi(state_dir)

    env = _base_env(state_dir, tmp_path)
    env.pop("VNX_SUPERVISOR_MODE", None)  # legacy (unset)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    env["VNX_LOG_CAPTURE"] = str(tmp_path / "log_capture.txt")

    proc = _run_bash("_maybe_oi_bridge_tick", env)
    assert proc.returncode == 0, proc.stderr
    assert "SURVIVED:0" in proc.stdout

    assert not (state_dir / ".oi_bridge_fresh").exists()
    assert _link_rows(state_dir, "OI-1") == []


# ---------------------------------------------------------------------------
# Bridge tick: links + idempotent second tick
# ---------------------------------------------------------------------------

def test_bridge_tick_links_new_oi_and_second_tick_is_idempotent(tmp_path):
    state_dir = tmp_path / "state"
    _build_db_v30(state_dir)
    _seed_blocking_oi(state_dir)

    env = _base_env(state_dir, tmp_path)
    env["VNX_OI_BRIDGE_INTERVAL"] = "0"  # never throttled within this test
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    env["VNX_LOG_CAPTURE"] = str(tmp_path / "log_capture.txt")

    # First tick — links the blocking OI.
    proc1 = _run_bash("_maybe_oi_bridge_tick", env)
    assert proc1.returncode == 0, proc1.stderr
    assert "SURVIVED:0" in proc1.stdout

    rows = _link_rows(state_dir, "OI-1")
    assert len(rows) == 1
    assert rows[0]["track_id"] == "T-bridge"
    assert rows[0]["link_type"] == "blocks"
    assert rows[0]["resolved_at"] is None

    fresh_file = state_dir / ".oi_bridge_fresh"
    assert fresh_file.read_text().strip() == "1"

    # Second tick — idempotent, no duplicate row.
    proc2 = _run_bash("_maybe_oi_bridge_tick", env)
    assert proc2.returncode == 0, proc2.stderr
    assert "SURVIVED:0" in proc2.stdout

    rows2 = _link_rows(state_dir, "OI-1")
    assert len(rows2) == 1, "second tick must not create a duplicate track_open_items row"
    assert fresh_file.read_text().strip() == "1"


# ---------------------------------------------------------------------------
# Bridge failure: survives, freshness signal flips to "0"
# ---------------------------------------------------------------------------

def test_bridge_tick_failure_survives_and_marks_not_fresh(tmp_path):
    """No open_items.json → BridgeSourceError (CLI exit 3). The tick must not
    crash the supervisor (best-effort) and must persist fresh="0"."""
    state_dir = tmp_path / "state"
    _build_db_v30(state_dir)
    # Deliberately no open_items.json written — source absent (C-N1, exit 3).

    env = _base_env(state_dir, tmp_path)
    env["VNX_OI_BRIDGE_INTERVAL"] = "0"
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    env["VNX_LOG_CAPTURE"] = str(tmp_path / "log_capture.txt")

    proc = _run_bash("_maybe_oi_bridge_tick", env)
    assert proc.returncode == 0, f"bridge failure must not crash the tick: {proc.stderr}"
    assert "SURVIVED:0" in proc.stdout

    fresh_file = state_dir / ".oi_bridge_fresh"
    assert fresh_file.exists()
    assert fresh_file.read_text().strip() == "0"

    log_capture = Path(env["VNX_LOG_CAPTURE"]).read_text()
    assert "WARN" in log_capture and "OI bridge tick failed" in log_capture


# ---------------------------------------------------------------------------
# Reconcile freshness gate: --apply withheld unless bridge signal == "1"
# ---------------------------------------------------------------------------

def _fake_python3(tmp_path: Path, capture_file: Path) -> Path:
    """A stub `python3` that captures argv (for planning_cli.py invocations
    only) and exits 0, so the reconcile leg's --apply gating can be asserted
    without running the (separately, exhaustively tested) real reconcile CLI."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "python3"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        printf '%s\\n' "$*" >> "{capture_file}"
        exit 0
        """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def test_reconcile_withholds_apply_when_bridge_fresh_file_absent(tmp_path):
    """No .oi_bridge_fresh file at all (bridge never attempted) fails CLOSED:
    --apply must not be appended even though VNX_AUTO_CLOSE defaults to on."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    capture = tmp_path / "reconcile_argv.txt"
    fakebin = _fake_python3(tmp_path, capture)

    env = _base_env(state_dir, tmp_path)
    env["VNX_OBJECTIVE_RECONCILE_INTERVAL"] = "0"
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    env["VNX_LOG_CAPTURE"] = str(tmp_path / "log_capture.txt")

    proc = _run_bash("_maybe_objective_reconcile", env)
    assert proc.returncode == 0, proc.stderr
    assert "SURVIVED:0" in proc.stdout

    argv = capture.read_text()
    assert "--apply" not in argv, f"--apply must be withheld with no bridge signal: {argv!r}"

    log_capture = Path(env["VNX_LOG_CAPTURE"]).read_text()
    assert "skipping --apply" in log_capture


def test_reconcile_withholds_apply_when_bridge_marked_not_fresh(tmp_path):
    """A bridge tick that explicitly failed this cycle (fresh="0") also gates
    --apply — the safety-critical bridge-failure <-> reconcile-freshness link."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / ".oi_bridge_fresh").write_text("0", encoding="utf-8")

    capture = tmp_path / "reconcile_argv.txt"
    fakebin = _fake_python3(tmp_path, capture)

    env = _base_env(state_dir, tmp_path)
    env["VNX_OBJECTIVE_RECONCILE_INTERVAL"] = "0"
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    env["VNX_LOG_CAPTURE"] = str(tmp_path / "log_capture.txt")

    proc = _run_bash("_maybe_objective_reconcile", env)
    assert proc.returncode == 0, proc.stderr

    argv = capture.read_text()
    assert "--apply" not in argv


def test_reconcile_includes_apply_when_bridge_marked_fresh(tmp_path):
    """A successful bridge run (fresh="1") allows --apply through, as before D1
    (VNX_AUTO_CLOSE default-on behaviour is preserved when data is fresh)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / ".oi_bridge_fresh").write_text("1", encoding="utf-8")

    capture = tmp_path / "reconcile_argv.txt"
    fakebin = _fake_python3(tmp_path, capture)

    env = _base_env(state_dir, tmp_path)
    env["VNX_OBJECTIVE_RECONCILE_INTERVAL"] = "0"
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    env["VNX_LOG_CAPTURE"] = str(tmp_path / "log_capture.txt")

    proc = _run_bash("_maybe_objective_reconcile", env)
    assert proc.returncode == 0, proc.stderr

    argv = capture.read_text()
    assert "--apply" in argv


def test_reconcile_never_applies_when_auto_close_explicitly_off(tmp_path):
    """VNX_AUTO_CLOSE=0 still suppresses --apply regardless of bridge freshness
    (pre-D1 opt-out is untouched by the new gate)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / ".oi_bridge_fresh").write_text("1", encoding="utf-8")

    capture = tmp_path / "reconcile_argv.txt"
    fakebin = _fake_python3(tmp_path, capture)

    env = _base_env(state_dir, tmp_path)
    env["VNX_OBJECTIVE_RECONCILE_INTERVAL"] = "0"
    env["VNX_AUTO_CLOSE"] = "0"
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    env["VNX_LOG_CAPTURE"] = str(tmp_path / "log_capture.txt")

    proc = _run_bash("_maybe_objective_reconcile", env)
    assert proc.returncode == 0, proc.stderr

    argv = capture.read_text()
    assert "--apply" not in argv


# ---------------------------------------------------------------------------
# Call-order guard: bridge tick runs BEFORE reconcile in process_dispatches()
# ---------------------------------------------------------------------------

def test_process_dispatches_calls_bridge_before_reconcile():
    """Static guard: _maybe_oi_bridge_tick must appear before
    _maybe_objective_reconcile in process_dispatches() — the reconciler's
    blocker-check must read the freshly-synced track_open_items from the SAME
    tick (plan-gate requirement, claudedocs/plan-oi-bridge-continuous.md)."""
    source = DISPATCHER_SH.read_text()
    match = re.search(
        r"^process_dispatches\(\) \{\n.*?^\}\n", source, re.MULTILINE | re.DOTALL,
    )
    assert match, "could not locate process_dispatches() in dispatcher_minimal.sh"
    body = match.group(0)

    bridge_pos = body.find("_maybe_oi_bridge_tick")
    reconcile_pos = body.find("_maybe_objective_reconcile")
    assert bridge_pos != -1, "process_dispatches() must call _maybe_oi_bridge_tick"
    assert reconcile_pos != -1, "process_dispatches() must call _maybe_objective_reconcile"
    assert bridge_pos < reconcile_pos, (
        "_maybe_oi_bridge_tick must run BEFORE _maybe_objective_reconcile in the same tick"
    )
