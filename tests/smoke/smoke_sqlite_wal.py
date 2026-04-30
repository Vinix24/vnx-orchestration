"""Smoke test - assert SQLite WAL files stay below the safe size cap.

Per the PR-T4 audit, ``runtime_coordination.db-wal`` was found at
~175 MB after the auto-checkpoint had stalled. Large WAL files indicate
either a checkpointer regression or a long-lived reader pinning the
log; either is a silent failure that warrants paging.

Behaviour:

  - Locate every ``*.db-wal`` file under ``$VNX_STATE_DIR``
  - If any exceeds ``MAX_WAL_BYTES`` (50 MB), attempt a TRUNCATE
    checkpoint on the matching ``.db`` and re-measure
  - Fail if the WAL is still oversize after checkpoint

When no DBs / WALs exist, the test is skipped (bare checkout).

Run:
    pytest tests/smoke/smoke_sqlite_wal.py -xvs
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

MAX_WAL_BYTES = 50 * 1024 * 1024  # 50 MB


def _resolve_state_dir() -> Path:
    try:
        from project_root import resolve_state_dir
        return resolve_state_dir(__file__)
    except Exception:
        env = os.environ.get("VNX_STATE_DIR")
        if env:
            return Path(env)
        data = os.environ.get("VNX_DATA_DIR")
        if data:
            return Path(data) / "state"
        return _REPO_ROOT / ".vnx-data" / "state"


def _wal_files(state_dir: Path) -> List[Path]:
    if not state_dir.exists():
        return []
    return sorted(state_dir.rglob("*.db-wal"))


def _checkpoint_truncate(db_path: Path) -> Tuple[bool, str]:
    if not db_path.exists():
        return False, f"db missing: {db_path}"
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.OperationalError as exc:
        return False, f"connect failed: {exc}"
    try:
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.DatabaseError as exc:
            return False, f"checkpoint pragma failed: {exc}"
        if row is None:
            return False, "checkpoint pragma returned no row"
        busy, log_pages, checkpointed_pages = row
        return busy == 0, (
            f"busy={busy} log_pages={log_pages} ckpt_pages={checkpointed_pages}"
        )
    finally:
        conn.close()


def test_wal_files_below_cap() -> None:
    state_dir = _resolve_state_dir()
    wals = _wal_files(state_dir)
    if not wals:
        pytest.skip(f"no *.db-wal files under {state_dir}")

    failures: List[str] = []
    actions: List[str] = []

    for wal in wals:
        size = wal.stat().st_size
        if size <= MAX_WAL_BYTES:
            continue

        db_path = wal.with_name(wal.name[: -len("-wal")])
        ok, info = _checkpoint_truncate(db_path)
        actions.append(
            f"{wal.name}: {size} bytes -> attempted TRUNCATE checkpoint "
            f"({'ok' if ok else 'failed'}, {info})"
        )

        try:
            new_size = wal.stat().st_size if wal.exists() else 0
        except OSError:
            new_size = size

        if new_size > MAX_WAL_BYTES:
            failures.append(
                f"{wal.name} still oversize after checkpoint: "
                f"{new_size} bytes > {MAX_WAL_BYTES} bytes "
                f"(db={db_path}, action={info})"
            )

    if actions:
        print("WAL checkpoint actions:")
        for line in actions:
            print(f"  - {line}")

    assert not failures, "WAL files exceed cap:\n  - " + "\n  - ".join(failures)


def main() -> int:
    state_dir = _resolve_state_dir()
    wals = _wal_files(state_dir)
    if not wals:
        print(f"SKIP: no WAL files under {state_dir}")
        return 0
    bad: List[str] = []
    for wal in wals:
        size = wal.stat().st_size
        if size <= MAX_WAL_BYTES:
            continue
        db_path = wal.with_name(wal.name[: -len("-wal")])
        _checkpoint_truncate(db_path)
        new_size = wal.stat().st_size if wal.exists() else 0
        if new_size > MAX_WAL_BYTES:
            bad.append(f"{wal.name}: {new_size} bytes")
    if bad:
        print("OVERSIZE WAL:")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("OK: WAL files under cap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
