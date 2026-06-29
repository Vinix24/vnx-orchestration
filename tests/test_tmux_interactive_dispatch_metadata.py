#!/usr/bin/env python3
"""Tests for leaseless tmux lane dispatch_metadata stamping (F1.1 metadata slice).

Covers:
- With VNX_TMUX_SESSION_ID=1, _govern_report stamps a dispatch_metadata row
  carrying provider='claude', role, model, terminal, track, outcome_status.
- With the flag unset, no row is written (legacy behaviour preserved).
- The metadata stamp is fail-open: a writer exception is swallowed and the
  dispatch/govern path returns normally.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT_LIB = REPO / "scripts" / "lib"
SCRIPT_DIR = REPO / "scripts"
for _p in (SCRIPT_LIB, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import quality_db_init  # noqa: E402
from tmux_interactive_dispatch import (  # noqa: E402
    TmuxInteractiveDispatch,
    TmuxResult,
)


class _FakeRunner:
    """Minimal runner stub so TmuxInteractiveDispatch never calls real tmux."""

    def available(self) -> bool:
        return True

    def run(self, args, *, timeout: int = 10, input_text: str | None = None) -> TmuxResult:
        return TmuxResult(0)


def _bootstrap_state(tmp_path: Path) -> Path:
    """Create a canonical vnx-dev state dir with a bootstrapped QI DB."""
    state_dir = tmp_path / ".vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / "quality_intelligence.db"
    assert quality_db_init.bootstrap_qi_db(db, REPO / "schemas" / "quality_intelligence.sql")
    return state_dir


def _make_lane(state_dir: Path) -> TmuxInteractiveDispatch:
    return TmuxInteractiveDispatch(
        state_dir,
        runner=_FakeRunner(),
        project_root=state_dir.parent.parent.parent,
    )


def _read_row(db: Path, dispatch_id: str):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM dispatch_metadata WHERE dispatch_id = ?", (dispatch_id,)
        ).fetchone()
    finally:
        conn.close()


@pytest.fixture
def fake_govern():
    """Patch dispatch_govern.govern to return a deterministic GovernedOutcome."""
    from dispatch_govern import GovernedOutcome  # noqa: PLC0415

    report_path = Path("/tmp/fake-report.md")
    outcome = GovernedOutcome(
        report_path=report_path,
        contract_status="authored",
    )
    with patch("dispatch_govern.govern", return_value=outcome):
        yield outcome


def test_metadata_row_written_when_session_id_flag_set(tmp_path, monkeypatch, fake_govern):
    monkeypatch.setenv("VNX_TMUX_SESSION_ID", "1")
    state_dir = _bootstrap_state(tmp_path)
    lane = _make_lane(state_dir)

    report_path = lane._govern_report(
        dispatch_id="20260628-tmux-meta-1",
        terminal_id="T1",
        instruction="do the thing",
        receipt={"status": "done"},
        duration_seconds=1.0,
        model="sonnet",
        role="backend-developer",
        pr_id="42",
    )

    assert report_path == fake_govern.report_path
    db = state_dir / "quality_intelligence.db"
    row = _read_row(db, "20260628-tmux-meta-1")
    assert row is not None
    assert row["provider"] == "claude"
    assert row["model"] == "sonnet"
    assert row["role"] == "backend-developer"
    assert row["terminal"] == "T1"
    assert row["track"] == "A"
    assert row["gate"] is None
    assert row["pr_id"] == "42"
    assert row["outcome_status"] == "authored"
    assert row["outcome_report_path"] == str(fake_govern.report_path)
    assert row["project_id"] == "vnx-dev"
    assert row["session_id"] is None


def test_metadata_row_not_written_when_flag_unset(tmp_path, monkeypatch, fake_govern):
    monkeypatch.delenv("VNX_TMUX_SESSION_ID", raising=False)
    state_dir = _bootstrap_state(tmp_path)
    lane = _make_lane(state_dir)

    lane._govern_report(
        dispatch_id="20260628-tmux-meta-0",
        terminal_id="T1",
        instruction="do the thing",
        receipt={"status": "done"},
        duration_seconds=1.0,
        model="sonnet",
        role="backend-developer",
    )

    db = state_dir / "quality_intelligence.db"
    row = _read_row(db, "20260628-tmux-meta-0")
    assert row is None


def test_metadata_stamp_fail_open_swallows_writer_error(
    tmp_path, monkeypatch, fake_govern, caplog
):
    monkeypatch.setenv("VNX_TMUX_SESSION_ID", "1")
    state_dir = _bootstrap_state(tmp_path)
    lane = _make_lane(state_dir)

    with patch(
        "tmux_interactive_dispatch._upsert_dispatch_metadata",
        side_effect=RuntimeError("DB unreachable"),
    ):
        with caplog.at_level("DEBUG", logger="tmux_interactive_dispatch"):
            report_path = lane._govern_report(
                dispatch_id="20260628-tmux-meta-fail",
                terminal_id="T2",
                instruction="do the thing",
                receipt={"status": "done"},
                duration_seconds=1.0,
                model="sonnet",
                role="debugger",
            )

    # Govern path must still complete normally.
    assert report_path == fake_govern.report_path
    # The swallowed error must be observable in DEBUG logs.
    assert any(
        "dispatch_metadata stamp failed" in r.message
        and "DB unreachable" in r.message
        for r in caplog.records
    )


def test_metadata_track_defaults_to_headless_for_non_terminal_label(
    tmp_path, monkeypatch, fake_govern
):
    monkeypatch.setenv("VNX_TMUX_SESSION_ID", "1")
    state_dir = _bootstrap_state(tmp_path)
    lane = _make_lane(state_dir)

    lane._govern_report(
        dispatch_id="20260628-tmux-meta-track",
        terminal_id="ephemeral",
        instruction="do the thing",
        receipt={"status": "done"},
        duration_seconds=1.0,
        model="sonnet",
        role="frontend-developer",
    )

    db = state_dir / "quality_intelligence.db"
    row = _read_row(db, "20260628-tmux-meta-track")
    assert row is not None
    assert row["terminal"] == "ephemeral"
    assert row["track"] == "headless"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
