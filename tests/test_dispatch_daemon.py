"""Tests for headless_dispatch_daemon.py — F48-PR1."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

# Ensure scripts/lib is on path
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from headless_dispatch_daemon import (
    DispatchDaemon,
    _is_terminal_available,
    _is_terminal_headless,
    parse_dispatch_metadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_data(tmp_path):
    """Create minimal VNX data directory layout."""
    (tmp_path / "dispatches" / "pending").mkdir(parents=True)
    (tmp_path / "dispatches" / "active").mkdir(parents=True)
    (tmp_path / "dispatches" / "completed").mkdir(parents=True)
    (tmp_path / "dispatches" / "dead_letter").mkdir(parents=True)
    (tmp_path / "state").mkdir(parents=True)
    return tmp_path


def _write_dispatch(pending_dir: Path, name: str, terminal: str = "T1",
                    track: str = "A", role: str = "backend-developer",
                    gate: str = "f48-pr1") -> Path:
    content = (
        f"[[TARGET:{terminal}]]\n"
        f"Track: {track}\n"
        f"Role: {role}\n"
        f"Gate: {gate}\n"
        f"\n---\n\n## Instruction\n\nDo the thing.\n"
    )
    p = pending_dir / name
    p.write_text(content)
    return p


def _write_t0_state(state_dir: Path, terminal: str, lease_state: str) -> None:
    state = {
        "schema_version": "2.0",
        "terminals": {
            terminal: {
                "lease_state": lease_state,
                "status": "idle",
            }
        },
    }
    (state_dir / "t0_state.json").write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# Metadata parsing
# ---------------------------------------------------------------------------

def test_parse_dispatch_metadata_extracts_all_fields(tmp_path):
    p = _write_dispatch(tmp_path, "20260413-dispatch-A.md", terminal="T2", role="test-engineer", gate="f48-pr2")
    meta = parse_dispatch_metadata(p)
    assert meta is not None
    assert meta.target_terminal == "T2"
    assert meta.track == "A"
    assert meta.role == "test-engineer"
    assert meta.gate == "f48-pr2"
    assert meta.dispatch_id == "20260413-dispatch-A"


def test_parse_dispatch_metadata_returns_none_without_target(tmp_path):
    p = tmp_path / "bad.md"
    p.write_text("# No target here\n\nJust some text.\n")
    assert parse_dispatch_metadata(p) is None


# ---------------------------------------------------------------------------
# Terminal availability
# ---------------------------------------------------------------------------

def test_terminal_available_when_idle(tmp_data):
    _write_t0_state(tmp_data / "state", "T1", "idle")
    assert _is_terminal_available("T1", tmp_data / "state") is True


def test_terminal_unavailable_when_leased(tmp_data):
    _write_t0_state(tmp_data / "state", "T1", "leased")
    assert _is_terminal_available("T1", tmp_data / "state") is False


def test_terminal_available_when_state_missing(tmp_data):
    # Missing t0_state.json → default available
    assert _is_terminal_available("T1", tmp_data / "state") is True


# ---------------------------------------------------------------------------
# test_picks_up_pending_dispatch
# ---------------------------------------------------------------------------

def test_picks_up_pending_dispatch(tmp_data, monkeypatch):
    """Place .md in pending/ — daemon detects within 10s (run_once)."""
    state_dir = tmp_data / "state"
    _write_t0_state(state_dir, "T1", "idle")
    _write_dispatch(tmp_data / "dispatches" / "pending", "20260413-test-A.md")

    # Patch headless check: T1 is headless
    monkeypatch.setenv("VNX_ADAPTER_T1", "subprocess")

    # Patch _acquire_lease to succeed without hitting runtime_core_cli
    monkeypatch.setattr(
        "headless_dispatch_daemon._acquire_lease",
        lambda terminal, dispatch_id: 42,
    )
    # Patch _deliver to succeed without subprocess
    monkeypatch.setattr(
        "headless_dispatch_daemon._deliver",
        lambda meta, active_path, state_dir: True,
    )
    # Patch _release_lease to succeed
    monkeypatch.setattr(
        "headless_dispatch_daemon._release_lease",
        lambda terminal, generation: True,
    )

    daemon = DispatchDaemon(data_dir=tmp_data, state_dir=state_dir, poll_interval=1.0)
    count = daemon.run_once()

    assert count == 1


# ---------------------------------------------------------------------------
# test_skips_busy_terminal
# ---------------------------------------------------------------------------

def test_skips_busy_terminal(tmp_data, monkeypatch):
    """Terminal lease_state=leased → daemon skips dispatch (no delivery)."""
    state_dir = tmp_data / "state"
    _write_t0_state(state_dir, "T1", "leased")
    _write_dispatch(tmp_data / "dispatches" / "pending", "20260413-busy-A.md")

    monkeypatch.setenv("VNX_ADAPTER_T1", "subprocess")

    deliver_calls = []
    monkeypatch.setattr(
        "headless_dispatch_daemon._deliver",
        lambda meta, active_path, state_dir: deliver_calls.append(meta) or True,
    )

    daemon = DispatchDaemon(data_dir=tmp_data, state_dir=state_dir)
    daemon.run_once()

    assert len(deliver_calls) == 0
    # File should still be in pending (deferred)
    pending = list((tmp_data / "dispatches" / "pending").iterdir())
    assert len(pending) == 1


# ---------------------------------------------------------------------------
# test_lifecycle_moves_files
# ---------------------------------------------------------------------------

def test_lifecycle_moves_files(tmp_data, monkeypatch):
    """Dispatch moves pending→active→completed through full lifecycle."""
    state_dir = tmp_data / "state"
    _write_t0_state(state_dir, "T1", "idle")
    _write_dispatch(tmp_data / "dispatches" / "pending", "20260413-lifecycle-A.md")

    monkeypatch.setenv("VNX_ADAPTER_T1", "subprocess")
    monkeypatch.setattr("headless_dispatch_daemon._acquire_lease", lambda t, d: 1)
    monkeypatch.setattr("headless_dispatch_daemon._deliver", lambda m, p, s: True)
    monkeypatch.setattr("headless_dispatch_daemon._release_lease", lambda t, g: True)

    daemon = DispatchDaemon(data_dir=tmp_data, state_dir=state_dir)
    daemon.run_once()

    pending = list((tmp_data / "dispatches" / "pending").iterdir())
    active = list((tmp_data / "dispatches" / "active").iterdir())
    completed = list((tmp_data / "dispatches" / "completed").iterdir())

    assert len(pending) == 0
    assert len(active) == 0
    assert len(completed) == 1
    assert completed[0].name == "20260413-lifecycle-A.md"


# ---------------------------------------------------------------------------
# test_audit_log_written
# ---------------------------------------------------------------------------

def test_audit_log_written(tmp_data, monkeypatch):
    """dispatch_audit.jsonl gets a record after delivery."""
    state_dir = tmp_data / "state"
    _write_t0_state(state_dir, "T1", "idle")
    _write_dispatch(tmp_data / "dispatches" / "pending", "20260413-audit-A.md",
                    gate="f48-audit", role="backend-developer")

    monkeypatch.setenv("VNX_ADAPTER_T1", "subprocess")
    monkeypatch.setattr("headless_dispatch_daemon._acquire_lease", lambda t, d: 7)
    monkeypatch.setattr("headless_dispatch_daemon._deliver", lambda m, p, s: True)
    monkeypatch.setattr("headless_dispatch_daemon._release_lease", lambda t, g: True)

    daemon = DispatchDaemon(data_dir=tmp_data, state_dir=state_dir)
    daemon.run_once()

    audit_path = tmp_data / "dispatch_audit.jsonl"
    assert audit_path.exists(), "dispatch_audit.jsonl not created"

    lines = [l for l in audit_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["dispatch_id"] == "20260413-audit-A"
    assert record["terminal"] == "T1"
    assert record["outcome"] == "done"
    assert record["gate"] == "f48-audit"
    assert record["lease_generation"] == 7


# ---------------------------------------------------------------------------
# test_skips_non_headless_terminal
# ---------------------------------------------------------------------------

def test_skips_non_headless_terminal(tmp_data, monkeypatch):
    """Non-subprocess terminal is skipped; no delivery attempted."""
    state_dir = tmp_data / "state"
    _write_t0_state(state_dir, "T1", "idle")
    _write_dispatch(tmp_data / "dispatches" / "pending", "20260413-tmux-A.md")

    # VNX_ADAPTER_T1 not set → tmux (default) → skip
    monkeypatch.delenv("VNX_ADAPTER_T1", raising=False)

    deliver_calls = []
    monkeypatch.setattr(
        "headless_dispatch_daemon._deliver",
        lambda meta, active_path, state_dir: deliver_calls.append(meta) or True,
    )

    daemon = DispatchDaemon(data_dir=tmp_data, state_dir=state_dir)
    daemon.run_once()

    assert len(deliver_calls) == 0
    # File stays in pending (no lifecycle move for skipped dispatches)
    pending = list((tmp_data / "dispatches" / "pending").iterdir())
    assert len(pending) == 1
