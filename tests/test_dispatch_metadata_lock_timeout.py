#!/usr/bin/env python3
"""Tests for the bounded sqlite lock-wait on the dispatch_metadata stamp.

Codex-gate finding on PR #1241: seam 4 removed the ``VNX_TMUX_SESSION_ID``
gate so ``upsert_dispatch_provider_row`` now runs on every claude-lane
dispatch. ``sqlite3.connect`` was called with no ``timeout=``, so a contended
DB fell back to the driver's default 5s lock-wait on a path that used to fire
almost never — turning "database is locked" into a per-dispatch stall.

Covers:
- The tmux lane's best-effort stamp now passes a short explicit timeout
  (``_METADATA_STAMP_LOCK_TIMEOUT_SECONDS``) and gives up well before the
  driver's 5s default when the DB is genuinely locked, measured by wall-clock
  elapsed time (not just "no exception escaped" — that was already true and
  is exactly what the finding says missed the actual point).
- Existing callers of ``upsert_dispatch_provider_row`` that don't pass
  ``timeout=`` keep the old wait behaviour: the default reproduces sqlite3's
  own 5.0s driver default, so a lock held for less than 5s still lets the
  write through.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT_LIB = REPO / "scripts" / "lib"
SCRIPT_DIR = REPO / "scripts"
for _p in (SCRIPT_LIB, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import quality_db_init  # noqa: E402
from dispatch_metadata_db import (  # noqa: E402
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    upsert_dispatch_provider_row,
)
from tmux_interactive_dispatch import (  # noqa: E402
    _METADATA_STAMP_LOCK_TIMEOUT_SECONDS,
    TmuxInteractiveDispatch,
    TmuxResult,
)


class _FakeRunner:
    def available(self) -> bool:
        return True

    def run(self, args, *, timeout: int = 10, input_text: str | None = None) -> TmuxResult:
        return TmuxResult(0)


def _bootstrap_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / "quality_intelligence.db"
    assert quality_db_init.bootstrap_qi_db(db, REPO / "schemas" / "quality_intelligence.sql")
    return db


def _hold_exclusive_lock(db_path: Path, hold_seconds: float, acquired: threading.Event) -> None:
    """Hold an EXCLUSIVE lock on ``db_path`` for ``hold_seconds`` on a side connection.

    Rollback-journal mode (the QI DB's default) makes an EXCLUSIVE transaction
    block every other connection's reads and writes until it commits — the
    same contention three parallel dispatches hit against this DB per the
    finding.
    """
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("BEGIN EXCLUSIVE")
    acquired.set()
    time.sleep(hold_seconds)
    conn.commit()
    conn.close()


def _read_row(db: Path, dispatch_id: str):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM dispatch_metadata WHERE dispatch_id = ?", (dispatch_id,)
        ).fetchone()
    finally:
        conn.close()


def test_default_lock_timeout_constant_matches_sqlite_driver_default():
    """sqlite3.connect's own undocumented-but-stable default is 5.0s — pin it as
    a named constant so it's never silently redefined out from under existing
    callers."""
    assert DEFAULT_LOCK_TIMEOUT_SECONDS == 5.0


def test_short_timeout_gives_up_fast_under_lock(tmp_path):
    """The tmux-lane's short timeout must bound the wait well under both the
    lock's hold time and the sqlite3 driver's 5s default — not merely avoid
    raising."""
    db = _bootstrap_state(tmp_path)
    lock_hold_seconds = 1.5
    short_timeout = 0.5
    acquired = threading.Event()
    holder = threading.Thread(
        target=_hold_exclusive_lock, args=(db, lock_hold_seconds, acquired)
    )
    holder.start()
    try:
        assert acquired.wait(timeout=2.0), "lock holder never acquired its lock"

        start = time.perf_counter()
        result = upsert_dispatch_provider_row(
            db,
            dispatch_id="lock-short-1",
            terminal="T1",
            provider="claude",
            timeout=short_timeout,
        )
        elapsed = time.perf_counter() - start

        assert result is False, "write must be skipped (fail-open), not raise"
        # Bounded well below the lock's 1.5s hold and sqlite3's 5.0s default —
        # this is the assertion the finding says the current test suite missed.
        assert elapsed < 1.2, f"expected a bounded wait near {short_timeout}s, got {elapsed:.3f}s"
    finally:
        holder.join(timeout=5)


def test_default_timeout_preserves_existing_caller_behavior(tmp_path):
    """A caller that omits ``timeout=`` (e.g. provider_dispatch.py's headless
    lane) must keep waiting out contention shorter than the 5s driver
    default — proving the new parameter didn't change unrelated callers."""
    db = _bootstrap_state(tmp_path)
    lock_hold_seconds = 1.0
    acquired = threading.Event()
    holder = threading.Thread(
        target=_hold_exclusive_lock, args=(db, lock_hold_seconds, acquired)
    )
    holder.start()
    try:
        assert acquired.wait(timeout=2.0), "lock holder never acquired its lock"

        start = time.perf_counter()
        result = upsert_dispatch_provider_row(
            db,
            dispatch_id="lock-default-1",
            terminal="T1",
            provider="claude",
            # timeout intentionally omitted — exercises the default.
        )
        elapsed = time.perf_counter() - start

        assert result is True, "default (5.0s) must outlast a 1.0s contention window"
        # Proves the wait actually happened (not an immediate no-op) —
        # the write only lands once the holder's commit releases the lock.
        assert elapsed >= lock_hold_seconds - 0.2
    finally:
        holder.join(timeout=5)

    row = _read_row(db, "lock-default-1")
    assert row is not None
    assert row["provider"] == "claude"


def _make_lane(state_dir: Path) -> TmuxInteractiveDispatch:
    return TmuxInteractiveDispatch(
        state_dir,
        runner=_FakeRunner(),
        project_root=state_dir.parent.parent.parent,
    )


@pytest.fixture
def fake_govern():
    from dispatch_govern import GovernedOutcome  # noqa: PLC0415

    outcome = GovernedOutcome(
        report_path=Path("/tmp/fake-report.md"),
        contract_status="authored",
    )
    from unittest.mock import patch  # noqa: PLC0415

    with patch("dispatch_govern.govern", return_value=outcome):
        yield outcome


def test_tmux_lane_metadata_stamp_bounds_wait_under_lock(tmp_path, monkeypatch, fake_govern):
    """End-to-end: TmuxInteractiveDispatch._govern_report's metadata stamp must
    not stall the dispatch behind a lock — the seam this PR fixes."""
    monkeypatch.delenv("VNX_TMUX_SESSION_ID", raising=False)
    state_dir = tmp_path / ".vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / "quality_intelligence.db"
    assert quality_db_init.bootstrap_qi_db(db, REPO / "schemas" / "quality_intelligence.sql")
    lane = _make_lane(state_dir)

    lock_hold_seconds = 1.5
    acquired = threading.Event()
    holder = threading.Thread(
        target=_hold_exclusive_lock, args=(db, lock_hold_seconds, acquired)
    )
    holder.start()
    try:
        assert acquired.wait(timeout=2.0), "lock holder never acquired its lock"

        start = time.perf_counter()
        report_path = lane._govern_report(
            dispatch_id="20260729-lock-tmux-1",
            terminal_id="T1",
            instruction="do the thing",
            receipt={"status": "done"},
            duration_seconds=1.0,
            model="sonnet",
            role="backend-developer",
        )
        elapsed = time.perf_counter() - start

        assert report_path == fake_govern.report_path, "dispatch must complete normally"
        assert elapsed < 1.2, f"metadata stamp stalled the dispatch: {elapsed:.3f}s"
    finally:
        holder.join(timeout=5)

    # The stamp genuinely lost the race against the lock — confirms the test
    # exercised real contention rather than a no-op.
    row = _read_row(db, "20260729-lock-tmux-1")
    assert row is None


def test_tmux_lane_uses_a_short_named_constant_not_the_driver_default():
    assert _METADATA_STAMP_LOCK_TIMEOUT_SECONDS < DEFAULT_LOCK_TIMEOUT_SECONDS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
