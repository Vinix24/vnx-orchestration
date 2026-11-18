"""ADR-075 decision 6, first step: under the writers' lock, ledger lines older than 90 days move to the archive, as bytes.

The decision goes to the receipt ledger first (ADR-005); any later failure restores every live file and archive byte for
byte and records "error". Lines without a timezone-aware ``ts`` and a torn tail stay live as they are. No archive removal.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import atomic_io
import permission_events as pe

LEDGERS = {"permission_events": "permission_events.ndjson", "cli_invocations": "cli_invocations.ndjson"}
LIVE_DAYS = 90
ARCHIVE_DIR = Path("archive") / "permission_events"


def _ts(line: bytes) -> Optional[datetime]:
    """The UTC time of a line, or None when it does not decode or has no readable, timezone-aware ``ts``."""
    try:
        when = datetime.fromisoformat(str(json.loads(line.decode("utf-8"))["ts"]).replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError, AttributeError):  # vnx-silent-except: None is counted as unparseable_ts, never moved
        return None
    return when.astimezone(timezone.utc) if when.tzinfo is not None else None


def _write_bytes(path: Path, data: bytes) -> None:
    """Atomic byte-exact write: latin-1 maps every byte 0-255 to one character and back, POSIX text mode adds no newline."""
    atomic_io.atomic_write_text(path, data.decode("latin-1"), encoding="latin-1")


def _append_archive(path: Path, lines: List[bytes]) -> None:
    """Add the lines the file does not hold yet (a retry after a crash adds nothing twice), fsynced."""
    have = {ln + b"\n" for ln in path.read_bytes().split(b"\n")} if path.exists() else set()
    new = [ln for ln in lines if ln not in have]
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        pe._append_all(path, b"".join(new))


def _plan(state: Path, ledger: str, now: datetime) -> Tuple[bytes, Optional[bytes], Dict[Path, List[bytes]], Dict[str, Any]]:
    """(original bytes or b"", new live bytes or None when nothing moves, archive lines per file, report)."""
    raw = (state / LEDGERS[ledger]).read_bytes() if (state / LEDGERS[ledger]).exists() else b""
    *done, tail = raw.split(b"\n")
    live: List[bytes] = []
    moved: Dict[Path, List[bytes]] = {}
    bad, oldest = 0, None
    for i, line in enumerate(done + [tail]):
        kept = line + b"\n" if i < len(done) else line  # the tail has no newline: it stays exactly as it is
        when = _ts(line) if line.strip() else None
        if when is not None and when < now - timedelta(days=LIVE_DAYS) and i < len(done):
            moved.setdefault(state / ARCHIVE_DIR / f"{ledger}-{when.year:04d}-{when.month:02d}.ndjson", []).append(kept)
            continue
        if when is None:
            bad += bool(line.strip())  # a blank line stays too, uncounted
        else:
            oldest = min(oldest, when) if oldest else when
        live.append(kept)
    count = sum(len(v) for v in moved.values())
    report = {"moved": count, "removed_archives": 0, "removed_reason": "removal_not_enabled", "unparseable_ts": bad,
              "oldest_live_ts": oldest.isoformat() if oldest else None}
    return raw, (b"".join(live) if count else None), moved, report


def _store(state: Path, project_id: str, entry: Dict[str, Any]) -> None:
    cov = state / "permission_coverage.json"
    sources, bad = pe._load_coverage(cov)
    sources["prune"] = entry
    pe._store_coverage(cov, project_id, sources, bad)


def _record_decision(state: Path, project_id: str, now: datetime, plans: Dict[str, tuple]) -> str:
    """Append the round's decision to the canonical receipt ledger; anything but "appended" raises."""
    import append_receipt  # late: the receipt stack is only needed for a round that moves lines
    receipt = {"timestamp": now.isoformat().replace("+00:00", "Z"), "event_type": "state_mutation",
               "receipt_kind": "state_mutation", "terminal": "T0", "source": "permission_events_prune",
               "file": ",".join(LEDGERS.values()), "trigger": "prune", "project_id": project_id, "round_id": uuid.uuid4().hex,
               "ledgers": {n: {str(a.relative_to(state)): len(ls) for a, ls in p[2].items()} for n, p in plans.items()}}
    result = append_receipt.append_receipt_payload(receipt, receipts_file=str(state / "t0_receipts.ndjson"),
                                                   skip_enrichment=True)
    if getattr(result, "status", None) != "appended":
        raise RuntimeError(f"prune decision receipt not appended: {getattr(result, 'status', result)!r}")
    return receipt["round_id"]


def prune(project_id: str, state_dir: Optional[Path] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """One retention round over both ledgers; returns the coverage entry. Raises after rolling back and writing "error"."""
    state = pe._state_dir(project_id, state_dir)
    now = now or datetime.now(timezone.utc)
    with pe._locked(state):
        entry: Dict[str, Any] = {"status": "ok", "updated": now.isoformat(), "ledgers": {}}
        lives: Dict[Path, bytes] = {}
        archives: Dict[Path, Optional[int]] = {}  # size before the round, None for a file this round creates
        try:
            plans = {name: _plan(state, name, now) for name in LEDGERS}
            if any(p[3]["moved"] for p in plans.values()):
                entry["round_id"] = _record_decision(state, project_id, now, plans)
            for _raw, _new, moved, _rep in plans.values():
                for apath, lines in moved.items():
                    archives[apath] = apath.stat().st_size if apath.exists() else None
                    _append_archive(apath, lines)
            for name, (raw, new, _moved, rep) in plans.items():
                if new is not None:
                    lives[state / LEDGERS[name]] = raw
                    _write_bytes(state / LEDGERS[name], new)
                entry["ledgers"][name] = rep
            _store(state, project_id, entry)
        except BaseException as exc:
            for path, raw in lives.items():  # every live file back to its bytes, every archive back to its old size
                _write_bytes(path, raw)
            for path, size in archives.items():
                os.truncate(path, size) if size is not None else path.unlink(missing_ok=True)
            _store(state, project_id, {"status": "error", "updated": now.isoformat(), "reason": f"{type(exc).__name__}: {exc}"[:200]})
            raise  # a failing error write raises its own exception, chained to this one
        return entry
