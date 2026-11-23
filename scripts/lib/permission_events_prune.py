"""ADR-075 decision 6 under the writers' lock: ledger lines older than 90 days move to the archive, as bytes, and then
an archive file goes once the last day of its month lies more than 365 days behind.

Move phase: the decision goes to the receipt ledger first (ADR-005); any later failure restores every live file and
archive byte for byte, each rollback step on its own (see ``_rollback``), and records "error". Lines without a timezone-aware ``ts`` and a
torn tail stay live as they are. Removal phase, after the move phase completed: see ``_remove``.
"""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import atomic_io
import permission_events as pe

LEDGERS = {"permission_events": "permission_events.ndjson", "cli_invocations": "cli_invocations.ndjson"}
LIVE_DAYS = 90
ARCHIVE_DIR = Path("archive") / "permission_events"
KEEP_DAYS = 365
ARCHIVE_NAME = re.compile(r"^(permission_events|cli_invocations)-(\d{4})-(\d{2})\.ndjson$")


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
    report = {"moved": count, "unparseable_ts": bad, "oldest_live_ts": oldest.isoformat() if oldest else None}
    return raw, (b"".join(live) if count else None), moved, report


def _store(state: Path, project_id: str, entry: Dict[str, Any]) -> None:
    cov = state / "permission_coverage.json"
    sources, bad = pe._load_coverage(cov)
    sources["prune"] = entry
    pe._store_coverage(cov, project_id, sources, bad)


def _record_decision(state: Path, project_id: str, now: datetime, plans: Dict[str, tuple]) -> str:
    ledgers = {n: {str(a.relative_to(state)): len(ls) for a, ls in p[2].items()} for n, p in plans.items()}
    return _append_decision(state, project_id, now, "prune", ",".join(LEDGERS.values()), ledgers=ledgers)


def _append_decision(state: Path, project_id: str, now: datetime, trigger: str, file: str, **detail: Any) -> str:
    """Append one decision to the canonical receipt ledger; anything but "appended" raises."""
    import append_receipt  # late: the receipt stack is only needed for a round that changes something
    receipt = {"timestamp": now.isoformat().replace("+00:00", "Z"), "event_type": "state_mutation",
               "receipt_kind": "state_mutation", "terminal": "T0", "source": "permission_events_prune",
               "file": file, "trigger": trigger, "project_id": project_id, "round_id": uuid.uuid4().hex, **detail}
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
            failed = _rollback(lives, archives)
            reason = "".join([f"{type(exc).__name__}: {exc}", *(f"; rollback failed: {f}" for f in failed)])
            try:
                _store(state, project_id, {"status": "error", "updated": now.isoformat(), "reason": reason[:1000]})
            except Exception as err:
                raise exc from err  # the original failure stays the one raised, the failed error write its cause
            raise
        entry["removal"] = _remove(state, project_id, now)
        for name, rep in entry["ledgers"].items():
            prefix = str(ARCHIVE_DIR / name) + "-"
            rep["removed_archives"] = sum(rel.startswith(prefix) for rel in entry["removal"]["removed"])
            rep["removed_reason"] = entry["removal"]["status"]
        _store(state, project_id, entry)
        return entry


def _rollback(lives: Dict[Path, bytes], archives: Dict[Path, Optional[int]]) -> List[str]:
    """Every live file back to its bytes, every archive back to its old size; each step runs, each failure is named.

    When a ledger's live restore fails, its archives from this round stay as they are, since the live file may no longer
    hold the moved lines. A line in both live and archive is a duplicate that ``_append_archive`` skips on a retry; a line
    in neither would be loss.
    """
    failed: List[str] = []
    broken: Dict[str, str] = {}  # ledger key -> live file name whose restore failed
    for path, raw in lives.items():
        try:
            _write_bytes(path, raw)
        except Exception as err:
            failed.append(f"restore {path.name}: {type(err).__name__}: {err}")
            broken.update({k: path.name for k, f in LEDGERS.items() if f == path.name})
    for path, size in archives.items():
        owner = next((k for k in broken if path.name.startswith(k + "-")), None)
        if owner is not None:
            failed.append(f"archive {path.name} kept: live restore of {broken[owner]} failed")
            continue
        try:
            os.truncate(path, size) if size is not None else path.unlink(missing_ok=True)
        except Exception as err:
            failed.append(f"archive {path.name}: {type(err).__name__}: {err}")
    return failed


def _expired(year: int, month: int, now: datetime) -> bool:
    """True when the month is valid and its last day lies more than KEEP_DAYS days before ``now`` (UTC dates)."""
    if not 1 <= month <= 12:
        return False
    try:
        last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    except (ValueError, OverflowError):  # vnx-silent-except: a month outside the calendar is never a removal candidate
        return False
    return (now.astimezone(timezone.utc).date() - last).days > KEEP_DAYS


def _refusal(raw: bytes, year: int, month: int) -> Optional[str]:
    """The first reason the content forbids removal, or None when every line has a ``ts`` in the file's own month."""
    if raw and not raw.endswith(b"\n"):
        return "torn_tail"
    for line in (ln for ln in raw.split(b"\n") if ln.strip()):
        when = _ts(line)
        if when is None:
            return "unparseable_ts"
        if (when.year, when.month) != (year, month):
            return "ts_outside_month"
    return None


def _remove(state: Path, project_id: str, now: datetime) -> Dict[str, Any]:
    """Removal phase (caller holds the lock): an expired archive file goes only when it is a regular file inside the store
    (lstat, never stat; a symlinked archive dir refuses the phase) and every line has a ``ts`` in the file's month.

    The decision is appended first; a failed append removes nothing. There is no rollback of a removal: each file was
    checked and recorded before it went. A failing unlink stops the phase as "error" and names what was not removed.
    """
    adir, rep, go = state / ARCHIVE_DIR, {"status": "ok", "removed": [], "refused": []}, {}
    try:
        if (state / "archive").is_symlink() or adir.is_symlink() or (
                adir.exists() and not adir.resolve(strict=True).is_relative_to(state.resolve(strict=True))):
            return {**rep, "status": "refused", "reason": "archive_dir_not_in_store"}
        for path in sorted(adir.iterdir()) if adir.exists() else []:
            m = ARCHIVE_NAME.fullmatch(path.name)
            if not m or not _expired(int(m[2]), int(m[3]), now):
                continue
            rel = str(path.relative_to(state))
            if not stat.S_ISREG(os.lstat(path).st_mode):
                rep["refused"].append({"path": rel, "reason": "not_a_regular_file"})
                continue
            with open(path, "rb", opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)) as fh:
                raw = fh.read()
            reason = _refusal(raw, int(m[2]), int(m[3]))
            if reason:
                rep["refused"].append({"path": rel, "reason": reason})
            else:
                go[rel] = {"bytes": len(raw), "lines": sum(bool(ln.strip()) for ln in raw.split(b"\n"))}
        if go:
            rep["round_id"] = _append_decision(state, project_id, now, "prune_remove", str(ARCHIVE_DIR), files=go)
        for rel in sorted(go):
            (state / rel).unlink(missing_ok=True)  # already gone is no error
            rep["removed"].append(rel)
    except Exception as exc:
        return {**rep, "status": "error", "reason": f"{type(exc).__name__}: {exc}"[:300],
                "not_removed": sorted(set(go) - set(rep["removed"]))}
    return rep
