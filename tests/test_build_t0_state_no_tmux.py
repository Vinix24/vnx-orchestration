#!/usr/bin/env python3
"""T-B1: the T0 state build never probes tmux.

The ``terminals`` block used to be filled through
``canonical_state_views.build_terminal_snapshot``, which reconciles from
``terminal_state.json`` and falls back to ``tmux list-panes -a``. T-B1 fills
it from the ``terminal_leases`` rows only, so a host without a tmux server
still builds a complete state document.

The tmux stub logs every invocation and exits 127 (as a missing tmux would):
a build that calls it fails on the log, not on an import.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(_REPO_ROOT / "scripts"), str(_REPO_ROOT / "scripts" / "lib")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import build_t0_state as bts  # noqa: E402

_TMUX_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> {log}
exit 127
"""


def _state_dirs(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / ".vnx-data"
    state_dir, dispatch_dir = data_dir / "state", data_dir / "dispatches"
    state_dir.mkdir(parents=True)
    dispatch_dir.mkdir(parents=True)
    (data_dir / ".vnx-project-id").write_text("vnx-dev\n", encoding="utf-8")
    return state_dir, dispatch_dir


def _install_tmux_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a logging, failing ``tmux`` first on PATH; return its call log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "tmux-calls.log"
    stub = bin_dir / "tmux"
    stub.write_text(_TMUX_STUB.format(log=log_path), encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log_path


def _tmux_calls(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    return [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _set_lease(
    state_dir: Path, terminal: str, state: str, *,
    dispatch_id: str | None = None, heartbeat: str | None = None,
) -> None:
    conn = sqlite3.connect(state_dir / "runtime_coordination.db")
    try:
        conn.execute(
            "UPDATE terminal_leases SET state = ?, dispatch_id = ?, leased_at = ?, "
            "last_heartbeat_at = ? WHERE terminal_id = ?",
            (state, dispatch_id, heartbeat, heartbeat, terminal),
        )
        conn.commit()
    finally:
        conn.close()


def test_state_build_never_calls_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir, dispatch_dir = _state_dirs(tmp_path)
    log_path = _install_tmux_stub(tmp_path, monkeypatch)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert state["schema_version"] == "2.2"
    assert _tmux_calls(log_path) == [], (
        "the state build shelled out to tmux: " + "; ".join(_tmux_calls(log_path))
    )

    live_work = state["live_work"]
    assert live_work["available"] is True, live_work.get("reason")
    assert set(live_work["counts"]) == {"live", "starting", "stale", "unmeasured"}
    assert live_work["counts"]["live"] == 0
    assert {"in_flight_states", "startup_grace_seconds", "open_prs"} <= set(live_work)

    # The block stays in this PR (T-B2 removes it) and is measured from leases.
    assert set(state["terminals"]) == {"T1", "T2", "T3"}
    assert state["terminals"]["T1"]["source"] == "lease"
    assert state["terminals"]["T1"]["lease_state"] == "idle"


def test_terminals_come_from_lease_rows_not_terminal_state_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh terminal_state.json must not override the lease rows (T-B1)."""
    state_dir, dispatch_dir = _state_dirs(tmp_path)
    _install_tmux_stub(tmp_path, monkeypatch)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    bts.build_t0_state(state_dir, dispatch_dir)

    (state_dir / "terminal_state.json").write_text(json.dumps({
        "schema_version": 1,
        "terminals": {"T2": {
            "terminal_id": "T2",
            "status": "working",
            "version": 1,
            "claimed_by": "dlv-from-terminal-state",
            "last_activity": datetime.now(timezone.utc).isoformat(),
        }},
    }), encoding="utf-8")
    _set_lease(state_dir, "T2", "idle")

    terminals = bts._build_terminals(state_dir)

    assert terminals["T2"]["status"] == "idle"
    assert terminals["T2"]["lease_state"] == "idle"
    assert terminals["T2"]["current_dispatch"] is None


def test_leased_row_is_working_with_its_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir, dispatch_dir = _state_dirs(tmp_path)
    _install_tmux_stub(tmp_path, monkeypatch)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    bts.build_t0_state(state_dir, dispatch_dir)
    _set_lease(
        state_dir, "T1", "leased",
        dispatch_id="dlv-leased", heartbeat="2026-10-27T10:05:00+00:00",
    )

    terminals = bts._build_terminals(state_dir)

    assert terminals["T1"]["status"] == "working"
    assert terminals["T1"]["ready"] is False
    assert terminals["T1"]["current_dispatch"] == "dlv-leased"
    assert terminals["T1"]["lease_state"] == "leased"
    assert terminals["T1"]["last_update"] == "2026-10-27T10:05:00+00:00"
    assert terminals["T1"]["status_age_seconds"] is not None


@pytest.mark.parametrize("unknown_state", ["bogus", ""])
def test_unknown_lease_state_is_never_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown_state: str
) -> None:
    state_dir, dispatch_dir = _state_dirs(tmp_path)
    _install_tmux_stub(tmp_path, monkeypatch)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    bts.build_t0_state(state_dir, dispatch_dir)
    _set_lease(state_dir, "T3", unknown_state)

    terminals = bts._build_terminals(state_dir)

    assert terminals["T3"]["status"] == "unknown"
    assert terminals["T3"]["ready"] is False
    assert terminals["T3"]["lease_state"] == (unknown_state or "unknown")


def test_unreadable_lease_table_is_unknown_with_a_reason(tmp_path: Path) -> None:
    """No lease table is unmeasured, not idle."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    terminals = bts._build_terminals(state_dir)

    for terminal in ("T1", "T2", "T3"):
        assert terminals[terminal]["status"] == "unknown"
        assert terminals[terminal]["ready"] is False
        assert terminals[terminal]["lease_state"] == "unknown"
        assert terminals[terminal]["lease_error"]
