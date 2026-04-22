#!/usr/bin/env python3
"""Tests for W0 PR4 — vnx snapshot/restore/quiesce-check tooling."""

from __future__ import annotations

import os
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
LIB_DIR = VNX_ROOT / "scripts" / "lib"

sys.path.insert(0, str(LIB_DIR))

from vnx_snapshot import do_quiesce_check, do_restore, do_snapshot


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_project(tmp_path: Path) -> Path:
    """Minimal project with .vnx-data layout."""
    vnx_data = tmp_path / ".vnx-data"
    state = vnx_data / "state"
    (vnx_data / "dispatches" / "active").mkdir(parents=True)
    (vnx_data / "dispatches" / "pending").mkdir(parents=True)
    state.mkdir(parents=True)
    (state / "t0_receipts.ndjson").write_text("{}\n")
    return tmp_path


@pytest.fixture()
def snapshot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override ~/vnx-snapshots to a temp dir."""
    snap_dir = tmp_path / "vnx-snapshots"
    snap_dir.mkdir()
    monkeypatch.setattr(
        "vnx_snapshot._snapshots_dir", lambda: snap_dir
    )
    return snap_dir


# ---------------------------------------------------------------------------
# snapshot tests
# ---------------------------------------------------------------------------


def test_snapshot_creates_tarball(fake_project: Path, snapshot_dir: Path):
    rc = do_snapshot(str(fake_project))
    assert rc == 0
    tarballs = list(snapshot_dir.glob("*.tar.gz"))
    assert len(tarballs) == 1, f"expected 1 tarball, got {tarballs}"
    assert tarfile.is_tarfile(str(tarballs[0]))


def test_snapshot_includes_vnx_data(fake_project: Path, snapshot_dir: Path):
    do_snapshot(str(fake_project))
    tarball = next(snapshot_dir.glob("*.tar.gz"))
    with tarfile.open(tarball, "r:gz") as tf:
        names = tf.getnames()
    assert any(n.startswith(".vnx-data") for n in names), f"missing .vnx-data in {names}"


def test_snapshot_missing_vnx_data_returns_error(tmp_path: Path, snapshot_dir: Path):
    rc = do_snapshot(str(tmp_path))
    assert rc == 1


# ---------------------------------------------------------------------------
# restore tests
# ---------------------------------------------------------------------------


def test_restore_roundtrip(fake_project: Path, snapshot_dir: Path, tmp_path: Path):
    # Snapshot
    do_snapshot(str(fake_project))
    tarball = next(snapshot_dir.glob("*.tar.gz"))

    # Remove original .vnx-data
    import shutil
    shutil.rmtree(fake_project / ".vnx-data")
    assert not (fake_project / ".vnx-data").exists()

    # Restore into the original project dir
    rc = do_restore(str(tarball), str(fake_project), force=True)
    assert rc == 0
    assert (fake_project / ".vnx-data").is_dir()
    # State file survives round-trip
    assert (fake_project / ".vnx-data" / "state" / "t0_receipts.ndjson").is_file()


def test_restore_rejects_non_tarball(tmp_path: Path):
    bad = tmp_path / "not_a_tarball.tar.gz"
    bad.write_bytes(b"not a tar")
    rc = do_restore(str(bad), str(tmp_path), force=True)
    assert rc == 1


def test_restore_rejects_tarball_without_vnx_data(tmp_path: Path):
    """Tarball that doesn't contain .vnx-data/ at root is rejected."""
    tarball = tmp_path / "wrong.tar.gz"
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("hello")
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(decoy, arcname="decoy.txt")
    rc = do_restore(str(tarball), str(tmp_path), force=True)
    assert rc == 1


# ---------------------------------------------------------------------------
# quiesce-check tests
# ---------------------------------------------------------------------------


def test_quiesce_check_clean(fake_project: Path):
    rc = do_quiesce_check(str(fake_project))
    assert rc == 0


def test_quiesce_check_active_dispatch_recent(fake_project: Path):
    """A recent .md in active/ → not quiescent."""
    dispatch = fake_project / ".vnx-data" / "dispatches" / "active" / "test-dispatch.md"
    dispatch.write_text("# dispatch\n")
    # File was just created — mtime is <1h ago
    rc = do_quiesce_check(str(fake_project))
    assert rc == 1


def test_quiesce_check_active_dispatch_old_is_ok(fake_project: Path):
    """An active dispatch older than 1 hour is treated as stale — quiescent."""
    dispatch = fake_project / ".vnx-data" / "dispatches" / "active" / "stale.md"
    dispatch.write_text("# stale\n")
    # Backdate mtime by 2 hours
    old_time = time.time() - 7200
    os.utime(dispatch, (old_time, old_time))
    rc = do_quiesce_check(str(fake_project))
    assert rc == 0


def test_quiesce_check_held_lease(fake_project: Path):
    """A 'leased' row in terminal_leases → not quiescent."""
    db_path = fake_project / ".vnx-data" / "state" / "runtime_coordination.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        "CREATE TABLE IF NOT EXISTS terminal_leases "
        "(terminal_id TEXT, state TEXT)"
    )
    con.execute("INSERT INTO terminal_leases VALUES ('T1', 'leased')")
    con.commit()
    con.close()
    rc = do_quiesce_check(str(fake_project))
    assert rc == 1


def test_quiesce_check_in_flight_gate(fake_project: Path):
    """A gate request without a result → not quiescent."""
    req_dir = fake_project / ".vnx-data" / "state" / "review_gates" / "requests"
    req_dir.mkdir(parents=True)
    (req_dir / "pr-42-codex.json").write_text('{"gate":"codex"}')
    rc = do_quiesce_check(str(fake_project))
    assert rc == 1


def test_quiesce_check_gate_with_result_is_ok(fake_project: Path):
    """Gate request with matching result → quiescent on that check."""
    req_dir = fake_project / ".vnx-data" / "state" / "review_gates" / "requests"
    res_dir = fake_project / ".vnx-data" / "state" / "review_gates" / "results"
    req_dir.mkdir(parents=True)
    res_dir.mkdir(parents=True)
    (req_dir / "pr-42-codex.json").write_text('{"gate":"codex"}')
    (res_dir / "pr-42-codex.json").write_text('{"result":"pass"}')
    rc = do_quiesce_check(str(fake_project))
    assert rc == 0
