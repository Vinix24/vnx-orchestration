"""Smoke test - assert dispatcher activity in the last 24h.

Verifies that the orchestration substrate is alive by checking three
independent activity sinks:

  - ``$VNX_STATE_DIR/t0_receipts.ndjson``       (T0 receipt stream)
  - ``$VNX_STATE_DIR/dispatch_register.ndjson`` (dispatcher event log)
  - ``intelligence_injections`` table in
    ``$VNX_STATE_DIR/runtime_coordination.db`` (intelligence selector audit)

A signal source is HEALTHY when it contains at least one record
timestamped within the last 24 hours. The test fails only if at least
one source exists but has gone silent — missing files / tables are
treated as "not yet provisioned" and skipped.

When the dispatcher is intentionally idle (weekend / freeze), set
``VNX_SMOKE_ALLOW_QUIET=1`` to convert silence into a skip rather than a
failure. CI cron is expected to set this flag on weekends.

Run:
    pytest tests/smoke/smoke_receipt_activity.py -xvs
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

ACTIVITY_WINDOW_SECONDS = 24 * 3600


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


def _allow_quiet() -> bool:
    return os.environ.get("VNX_SMOKE_ALLOW_QUIET") == "1"


def _parse_ts(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        import datetime as _dt
        cleaned = s.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _ndjson_has_recent_record(
    path: Path,
    cutoff: float,
    ts_keys: Tuple[str, ...] = ("ts", "timestamp", "created_at", "emitted_at"),
) -> Tuple[bool, int]:
    """Return (has_recent, total_lines). Reads the file forward; for the
    sizes we expect (<10 MB) this is cheap and avoids ndjson tail edge
    cases. ``cutoff`` is a unix epoch seconds value: records strictly
    above it count as recent.
    """
    has_recent = False
    total = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                ts: Optional[float] = None
                for key in ts_keys:
                    if key in record:
                        ts = _parse_ts(record[key])
                        if ts is not None:
                            break
                if ts is not None and ts > cutoff:
                    has_recent = True
    except OSError:
        return False, 0
    return has_recent, total


def _intelligence_injections_recent(db_path: Path, cutoff: float) -> Tuple[bool, int]:
    """Return (has_recent_row, total_rows). Returns (False, 0) when DB or
    table is missing — callers must treat that as 'not provisioned'.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return False, 0
    try:
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='intelligence_injections'"
            )
            if cur.fetchone() is None:
                return False, 0
        except sqlite3.DatabaseError:
            return False, 0

        candidate_columns = ("created_at", "ts", "timestamp", "injected_at")
        ts_col = None
        try:
            cur = conn.execute("PRAGMA table_info(intelligence_injections)")
            cols = {row[1] for row in cur.fetchall()}
        except sqlite3.DatabaseError:
            cols = set()
        for c in candidate_columns:
            if c in cols:
                ts_col = c
                break

        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM intelligence_injections"
            ).fetchone()[0]
        except sqlite3.DatabaseError:
            total = 0

        if ts_col is None:
            return total > 0, int(total)

        try:
            cutoff_iso_z = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff)
            )
            row = conn.execute(
                f"SELECT COUNT(*) FROM intelligence_injections "
                f"WHERE {ts_col} > ? OR {ts_col} > ?",
                (cutoff, cutoff_iso_z),
            ).fetchone()
            recent = int(row[0]) if row else 0
        except sqlite3.DatabaseError:
            recent = 0

        return recent > 0, int(total)
    finally:
        conn.close()


def test_dispatcher_activity_recent() -> None:
    state_dir = _resolve_state_dir()
    cutoff = time.time() - ACTIVITY_WINDOW_SECONDS

    receipts = state_dir / "t0_receipts.ndjson"
    register = state_dir / "dispatch_register.ndjson"
    coord_db = state_dir / "runtime_coordination.db"

    sources: List[Tuple[str, Path, bool, bool, int]] = []

    if receipts.exists():
        recent, total = _ndjson_has_recent_record(receipts, cutoff)
        sources.append(("t0_receipts.ndjson", receipts, True, recent, total))
    else:
        sources.append(("t0_receipts.ndjson", receipts, False, False, 0))

    if register.exists():
        recent, total = _ndjson_has_recent_record(register, cutoff)
        sources.append(("dispatch_register.ndjson", register, True, recent, total))
    else:
        sources.append(("dispatch_register.ndjson", register, False, False, 0))

    if coord_db.exists():
        recent, total = _intelligence_injections_recent(coord_db, cutoff)
        sources.append(("intelligence_injections", coord_db, True, recent, total))
    else:
        sources.append(("intelligence_injections", coord_db, False, False, 0))

    provisioned = [s for s in sources if s[2]]
    if not provisioned:
        pytest.skip(
            f"no activity sources provisioned under {state_dir} "
            "(bare environment / fresh checkout)"
        )

    silent: List[str] = []
    for name, path, present, recent, total in provisioned:
        if not recent:
            silent.append(f"{name}: total={total}, none in last 24h ({path})")

    if silent and _allow_quiet():
        pytest.skip(
            "VNX_SMOKE_ALLOW_QUIET=1; tolerated quiet sources:\n  - "
            + "\n  - ".join(silent)
        )

    assert not silent, (
        "dispatcher activity sources have been silent for >24h:\n  - "
        + "\n  - ".join(silent)
    )


def main() -> int:
    state_dir = _resolve_state_dir()
    cutoff = time.time() - ACTIVITY_WINDOW_SECONDS
    bad: List[str] = []
    found_any = False
    for name, path in (
        ("t0_receipts.ndjson", state_dir / "t0_receipts.ndjson"),
        ("dispatch_register.ndjson", state_dir / "dispatch_register.ndjson"),
    ):
        if not path.exists():
            continue
        found_any = True
        recent, total = _ndjson_has_recent_record(path, cutoff)
        if not recent:
            bad.append(f"{name}=silent (total={total})")
    coord_db = state_dir / "runtime_coordination.db"
    if coord_db.exists():
        found_any = True
        recent, total = _intelligence_injections_recent(coord_db, cutoff)
        if not recent:
            bad.append(f"intelligence_injections=silent (total={total})")

    if not found_any:
        print(f"SKIP: no activity sources provisioned under {state_dir}")
        return 0
    if bad and not _allow_quiet():
        print("SILENT:")
        for line in bad:
            print(f"  - {line}")
        return 1
    print("OK: dispatcher activity recent" if not bad else "OK (quiet allowed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
