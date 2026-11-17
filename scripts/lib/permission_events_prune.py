"""Retention of the permission ledgers (ADR-075 decision 6): 90 days live, a year in the archive, then removed.

``prune`` runs under the lock every writer takes. Archive lines are written and fsynced first, then both new live
files are renamed into place; a failure restores the live files byte for byte and leaves status "error" in the coverage.
A line without a timezone-aware ``ts`` (and a torn last line) is never moved or removed: it counts as unparseable_ts.
"""
from __future__ import annotations

import calendar
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import atomic_io
import permission_events as pe

LEDGERS = {"permission_events": "permission_events.ndjson", "cli_invocations": "cli_invocations.ndjson"}
LIVE_DAYS, ARCHIVE_DAYS = 90, 365
ARCHIVE_DIR = Path("archive") / "permission_events"
COVERAGE_KEY = "prune"
_ARCHIVE_NAME = re.compile(r"(?P<ledger>[a-z_]+)-(?P<y>\d{4})-(?P<m>\d{2})\.ndjson")


def _ts(line: bytes) -> Optional[datetime]:
    """The UTC time of a line, or None when it has no readable, timezone-aware ``ts``."""
    try:
        when = datetime.fromisoformat(str(json.loads(line.decode("utf-8"))["ts"]).replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError, AttributeError):  # vnx-silent-except: None is counted as unparseable_ts, never moved
        return None
    return when.astimezone(timezone.utc) if when.tzinfo is not None else None


def _archive_path(state: Path, ledger: str, when: datetime) -> Path:
    return state / ARCHIVE_DIR / f"{ledger}-{when.year:04d}-{when.month:02d}.ndjson"


def _append_archive(path: Path, lines: List[bytes]) -> None:
    """Add the lines the file does not hold yet (a retry after a failed round adds nothing twice), fsynced."""
    have = set(path.read_bytes().split(b"\n")) if path.exists() else set()
    new = [ln for ln in lines if ln not in have]
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        pe._append_all(path, b"".join(ln + b"\n" for ln in new))


def _plan(state: Path, ledger: str, now: datetime) -> Tuple[bytes, Optional[bytes], Dict[Path, List[bytes]], Dict[str, Any]]:
    """(original bytes or b"", new live bytes or None when nothing moves, archive lines per file, report)."""
    path = state / LEDGERS[ledger]
    raw = path.read_bytes() if path.exists() else b""
    cutoff = now - timedelta(days=LIVE_DAYS)
    live: List[bytes] = []
    moved: Dict[Path, List[bytes]] = {}
    bad, oldest = 0, None
    for i, line in enumerate(parts := raw.split(b"\n")):
        if not line.strip():
            continue
        when = _ts(line)
        if when is None:
            bad += 1
            live.append(line)
        elif when < cutoff and not (i == len(parts) - 1):  # the last part has no newline: a torn tail stays where it is
            moved.setdefault(_archive_path(state, ledger, when), []).append(line)
        else:
            live.append(line)
            oldest = min(oldest, when) if oldest else when
    count = sum(len(v) for v in moved.values())
    report = {"moved": count, "removed_archives": [], "unparseable_ts": bad,
              "oldest_live_ts": oldest.isoformat() if oldest else None}
    new = b"".join(ln + b"\n" for ln in live) if count else None
    return raw, new, moved, report


def _expired(state: Path, ledger: str, now: datetime) -> List[Path]:
    out = []
    for f in sorted((state / ARCHIVE_DIR).glob(f"{ledger}-*.ndjson")):
        m = _ARCHIVE_NAME.fullmatch(f.name)
        if not m or m["ledger"] != ledger or not 1 <= int(m["m"]) <= 12:
            continue  # not an archive file of this ledger: left alone
        y, mo = int(m["y"]), int(m["m"])
        last = datetime(y, mo, calendar.monthrange(y, mo)[1], tzinfo=timezone.utc) + timedelta(days=1)  # end of the last day
        if now - last > timedelta(days=ARCHIVE_DAYS):
            out.append(f)
    return out


def _store(state: Path, project_id: str, entry: Dict[str, Any]) -> None:
    cov = state / "permission_coverage.json"
    sources, bad = pe._load_coverage(cov)
    sources[COVERAGE_KEY] = entry
    pe._store_coverage(cov, project_id, sources, bad)


def prune(project_id: str, state_dir: Optional[Path] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """One retention round over both ledgers; returns the coverage entry. Raises after writing status "error"."""
    state = pe._state_dir(project_id, state_dir)
    now = now or datetime.now(timezone.utc)
    with pe._locked(state):
        entry: Dict[str, Any] = {"status": "ok", "updated": now.isoformat(), "ledgers": {}}
        originals: Dict[Path, bytes] = {}
        replaced: List[Path] = []
        try:
            plans = {name: _plan(state, name, now) for name in LEDGERS}
            for _raw, _new, moved, _rep in plans.values():
                for apath, lines in moved.items():
                    _append_archive(apath, lines)
            for name, (raw, new, _moved, rep) in plans.items():
                if new is not None:
                    originals[state / LEDGERS[name]] = raw
                    atomic_io.atomic_write_text(state / LEDGERS[name], new.decode("utf-8"))
                    replaced.append(state / LEDGERS[name])
                rep["removed_archives"] = [f.name for f in _expired(state, name, now)]
                entry["ledgers"][name] = rep
            for f in [f for n in LEDGERS for f in _expired(state, n, now)]:
                f.unlink()
        except BaseException as exc:
            for path in replaced:  # both ledgers back to their bytes; the archive is idempotent on the retry
                atomic_io.atomic_write_text(path, originals[path].decode("utf-8"))
            _store(state, project_id, {"status": "error", "updated": now.isoformat(), "reason": f"{type(exc).__name__}: {exc}"[:200]})
            raise
        _store(state, project_id, entry)
        return entry

