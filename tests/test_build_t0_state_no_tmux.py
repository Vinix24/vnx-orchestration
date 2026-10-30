#!/usr/bin/env python3
"""T-B1/T-B2: the T0 state build never probes tmux and has no terminals block.

The ``terminals`` block used to be filled through
``canonical_state_views.build_terminal_snapshot``, which reconciles from
``terminal_state.json`` and falls back to ``tmux list-panes -a``. T-B1 filled
it from the ``terminal_leases`` rows only; T-B2 removed the block entirely —
what is running is ``live_work``.

The tmux stub logs every invocation and exits 127 (as a missing tmux would):
a build that calls it fails on the log, not on an import.
"""

from __future__ import annotations

import os
import sys
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


def test_state_has_no_terminals_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-B2: what is running is live_work; there is no per-terminal block."""
    state_dir, dispatch_dir = _state_dirs(tmp_path)
    _install_tmux_stub(tmp_path, monkeypatch)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

    state = bts.build_t0_state(state_dir, dispatch_dir)
    assert "terminals" not in state

    index = bts._build_t0_index(state)
    assert "terminals" not in index


def test_brief_has_no_terminals_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The brief adapter has no terminals reader left to feed."""
    state_dir, dispatch_dir = _state_dirs(tmp_path)
    _install_tmux_stub(tmp_path, monkeypatch)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

    state = bts.build_t0_state(state_dir, dispatch_dir)
    brief = bts._state_to_brief(state)
    assert "terminals" not in brief
