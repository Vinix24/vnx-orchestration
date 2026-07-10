"""Tests for quality_intelligence.db backup rotation (VNX_DB_BACKUP_KEEP)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_LIB = _SCRIPTS / "lib"
for _p in (_SCRIPTS, _LIB):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("VNX_HOME", str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("VNX_DATA_DIR", str(Path(__file__).resolve().parent.parent / ".vnx-data"))
os.environ.setdefault("VNX_STATE_DIR", str(Path(__file__).resolve().parent.parent / ".vnx-data/state"))

import quality_db_init as qd


def _make_backups(state_dir: Path, count: int) -> list[Path]:
    """Create ``count`` backup files with strictly increasing mtimes."""
    backups: list[Path] = []
    base_time = 1_000_000_000  # fixed epoch baseline avoids filesystem noise
    for i in range(count):
        ts = f"20260710_{i:06d}"
        bp = state_dir / f"quality_intelligence.db.backup_{ts}"
        bp.write_text(f"backup {i}")
        os.utime(bp, (base_time + i, base_time + i))
        backups.append(bp)
    return backups


def test_backup_rotation_keeps_last_n(tmp_path, monkeypatch):
    """backup_existing_db prunes older backups, keeps VNX_DB_BACKUP_KEEP newest."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    db_path = state_dir / "quality_intelligence.db"
    db_path.write_text("live db")

    premigrate = state_dir / "quality_intelligence.db.pre-migrate-xyz"
    premigrate.write_text("pre-migrate copy")

    unrelated = state_dir / "unrelated_file.txt"
    unrelated.write_text("leave me alone")

    # 6 pre-existing backups (keep=3 means 3 will survive after the new backup is added).
    pre_backups = _make_backups(state_dir, 6)
    # Attach sidecars to every other backup so we can verify sidecar pruning.
    for i, bp in enumerate(pre_backups):
        if i % 2 == 0:
            (state_dir / f"{bp.name}-wal").write_text("wal")
            (state_dir / f"{bp.name}-shm").write_text("shm")

    monkeypatch.setattr(qd, "STATE_DIR", state_dir)
    monkeypatch.setattr(qd, "DB_PATH", db_path)
    monkeypatch.setenv("VNX_DB_BACKUP_KEEP", "3")

    assert qd.backup_existing_db() is True

    db_backups = sorted(
        (
            p for p in state_dir.glob(f"{qd._BACKUP_PREFIX}*")
            if not (p.name.endswith("-wal") or p.name.endswith("-shm"))
        ),
        key=lambda p: p.stat().st_mtime,
    )

    # 3 newest remain: the just-created backup plus the 2 newest pre-existing ones.
    assert len(db_backups) == 3
    kept_names = {p.name for p in db_backups}
    assert pre_backups[-2].name in kept_names
    assert pre_backups[-1].name in kept_names

    # Oldest 4 pre-existing backups and their sidecars were pruned.
    for old in pre_backups[:-2]:
        assert not old.exists()
        assert not (state_dir / f"{old.name}-wal").exists()
        assert not (state_dir / f"{old.name}-shm").exists()

    # Sidecars of the kept pre-existing backups survive.
    for kept in pre_backups[-2:]:
        if (state_dir / f"{kept.name}-wal").exists() or (state_dir / f"{kept.name}-shm").exists():
            assert (state_dir / f"{kept.name}-wal").exists()
            assert (state_dir / f"{kept.name}-shm").exists()

    # Live DB, pre-migrate copy, and unrelated files are untouched.
    assert db_path.read_text() == "live db"
    assert premigrate.read_text() == "pre-migrate copy"
    assert unrelated.read_text() == "leave me alone"


def test_backup_rotation_invalid_env_falls_back_to_three(tmp_path, monkeypatch):
    """Invalid VNX_DB_BACKUP_KEEP falls back to keep=3."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    db_path = state_dir / "quality_intelligence.db"
    db_path.write_text("live db")

    pre_backups = _make_backups(state_dir, 5)

    monkeypatch.setattr(qd, "STATE_DIR", state_dir)
    monkeypatch.setattr(qd, "DB_PATH", db_path)
    monkeypatch.setenv("VNX_DB_BACKUP_KEEP", "not-a-number")

    assert qd.backup_existing_db() is True

    db_backups = [
        p for p in state_dir.glob(f"{qd._BACKUP_PREFIX}*")
        if not (p.name.endswith("-wal") or p.name.endswith("-shm"))
    ]
    assert len(db_backups) == 3


def test_backup_rotation_zero_env_falls_back_to_three(tmp_path, monkeypatch):
    """VNX_DB_BACKUP_KEEP=0 falls back to keep=3 and does not delete everything."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    db_path = state_dir / "quality_intelligence.db"
    db_path.write_text("live db")

    pre_backups = _make_backups(state_dir, 5)

    monkeypatch.setattr(qd, "STATE_DIR", state_dir)
    monkeypatch.setattr(qd, "DB_PATH", db_path)
    monkeypatch.setenv("VNX_DB_BACKUP_KEEP", "0")

    assert qd.backup_existing_db() is True

    db_backups = [
        p for p in state_dir.glob(f"{qd._BACKUP_PREFIX}*")
        if not (p.name.endswith("-wal") or p.name.endswith("-shm"))
    ]
    assert len(db_backups) == 3


def test_backup_rotation_no_op_when_under_limit(tmp_path, monkeypatch):
    """Nothing is pruned when the number of backups is already <= keep."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    db_path = state_dir / "quality_intelligence.db"
    db_path.write_text("live db")

    pre_backups = _make_backups(state_dir, 2)

    monkeypatch.setattr(qd, "STATE_DIR", state_dir)
    monkeypatch.setattr(qd, "DB_PATH", db_path)
    monkeypatch.setenv("VNX_DB_BACKUP_KEEP", "3")

    assert qd.backup_existing_db() is True

    db_backups = [
        p for p in state_dir.glob(f"{qd._BACKUP_PREFIX}*")
        if not (p.name.endswith("-wal") or p.name.endswith("-shm"))
    ]
    # 2 pre-existing + 1 newly created = 3, all kept.
    assert len(db_backups) == 3
    for pb in pre_backups:
        assert pb.exists()


def test_rotate_helper_uses_filename_timestamp_not_mtime():
    """Rotation ranks backups by the filename timestamp, not mtime.

    shutil.copy2 preserves the *source* DB's mtime, so a newer backup can have
    an OLDER mtime than an older one. Here the mtime order is deliberately the
    inverse of the filename-timestamp order: the filename-newest backup
    (2026-07-04) is given the OLDEST mtime, and the filename-oldest (2026-06-01)
    the newest mtime. Correct rotation must keep the filename-newest; an
    mtime-based sort would wrongly keep the stale one and could even delete the
    just-created backup.
    """
    state_dir = Path(__file__).resolve().parent / "_rotate_helper_tmp"
    state_dir.mkdir(exist_ok=True)
    try:
        newest_by_name = state_dir / "quality_intelligence.db.backup_20260704_020001"
        oldest_by_name = state_dir / "quality_intelligence.db.backup_20260601_020001"
        newest_by_name.write_text("newest backup")
        oldest_by_name.write_text("oldest backup")
        # Inverted mtimes: filename-newest gets the OLDEST mtime.
        os.utime(newest_by_name, (1_000_000_000, 1_000_000_000))
        os.utime(oldest_by_name, (2_000_000_000, 2_000_000_000))

        qd._rotate_quality_db_backups(state_dir, keep=1)

        assert newest_by_name.exists(), "filename-newest backup must be kept"
        assert not oldest_by_name.exists(), "filename-oldest backup must be pruned"
    finally:
        for p in state_dir.glob(f"{qd._BACKUP_PREFIX}*"):
            p.unlink(missing_ok=True)
        state_dir.rmdir()
