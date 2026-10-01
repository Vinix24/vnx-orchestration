#!/usr/bin/env python3
"""open_outcomes.py — the dispatches a T0 still has to decide on (fabric-state-herstel D4b).

A dispatch is only finished with an outcome (operator decision 29-09-2026).
``receipt_outcome`` folds a dispatch's receipts into one outcome; a ``reject``
or ``investigate`` is not the end of it but a point a T0 has to look at. This
module lists those points and reads the decisions a T0 recorded about them.

It replaces the byte cursor of ``receipt_query.py pull``: a shared cursor let
the first T0 that pulled read a receipt away from every other T0, and a
receipt that was read was no longer visible anywhere. Here nothing is
consumed. An open outcome stays open for every reader until a T0 records a
decision about that dispatch in ``t0_decision_log.jsonl``.

Decisions
---------
One JSON line per decision, written only through ``t0_decision_log.
write_decision`` (append under an exclusive ``flock``, never a
read-modify-write): ``decision_type: "outcome_decision"``, ``dispatch_id``,
``project_id``, ``decision`` (``accept`` | ``reject``), ``timestamp``. Two
decisions about one dispatch may exist; the last one in file order counts.
An incomplete last line (a writer mid-append) and a malformed line are
skipped, never a crash. A decision of another project never closes an open
outcome of this one (ADR-007: the decision carries ``project_id`` and the
reader filters on it). Other records in the log (``dispatch_created``,
``pr_merge``, ...) are no outcome decision.

Scope
-----
Only dispatches with a receipt at or after ``OPEN_OUTCOMES_EPOCH`` are
listed. Before the per-dispatch reader (``OUTCOME_READER_EPOCH``) an
``investigate`` was a per-line stamp on bookkeeping, not a judgment, so the
backlog before it is not a list of open points. It stays in the ledger,
reachable through ``receipt_query.py by-dispatch``/``since``.

Each item carries ``kind``, counted in ``by_kind``:

* ``receipt_outcome`` — a dispatch in the ledger whose outcome is
  ``reject``, ``investigate`` or ``unknown`` (only evidence, no outcome line
  of its own). A ``superseded`` dispatch is no open point: the child that
  took the work over carries the outcome.
* ``active_dispatch`` (D4b2) — a dispatch still in a live bucket as an
  ``<id>/`` directory or as the ``<id>.md`` a failed delivery leaves
  (``scan_active``/``scan_rejected``; a ``.md`` without a ``[[TARGET:...]]``
  marker is no dispatch and only counted in ``ignored``), that the
  active-drain leaves standing without an outcome: no receipt past
  the threshold (``no_receipt``), a receipt without an outcome of its own
  (``no_outcome``), or a receipt outcome other than accept (``reject``,
  ``investigate``). ``active_destination`` is the one rule for both the
  drain and this list, and ``receipt_presence`` the one test of "has a
  receipt"; the janitor and dispatch_cleanup read the same two.
* ``abandoned_dispatch`` — a bundle ``dispatch_cleanup.py`` moved out of
  ``dispatches/pending/`` into ``dispatches/abandoned/`` (a
  ``stale-no-receipt`` bundle: no receipt, at least 7 days old) that no
  outcome decision of this project has closed. ``abandoned/`` stays the
  storage bucket cleanup moves it to; the bucket is not an outcome. A
  bundle that cannot be read is listed too: an open point is safer than a
  silent one.

  The live buckets are ``dispatches/active/`` **and**
  ``dispatches/rejected/<reason>/``: the post-worker-exit cleanup
  (``cleanup_worker_exit._move_dispatch_file_step``) moves a non-success
  exit's dispatch file there by itself, with no T0 decision behind it, so
  the bucket is storage, never an ending — the dispatch stays an open point
  until a T0 records accept or reject for it (``scan_open_buckets``).

Nothing without an outcome ends in ``completed/`` or ``dead_letter/``: an
accept goes to completed, dead_letter is only a T0 ``reject`` in the
decision log (operator decision 29-09-2026).

No database is read and no table is added: the per-project ledger and
decision log plus the ``project_id`` filter are the whole scope.

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, NamedTuple, Optional

from receipt_outcome import (
    BOOKKEEPING_EVENT_TYPES,
    INVALID_DISPATCH_IDS,
    OUTCOME_READER_EPOCH,
    noise_reason,
    summarize,
)

LEDGER_NAME = "t0_receipts.ndjson"
DECISION_LOG_NAME = "t0_decision_log.jsonl"

# Dispatches with a receipt at or after this moment are listed; the backlog
# before the per-dispatch reader is left out (see the module docstring).
OPEN_OUTCOMES_EPOCH = OUTCOME_READER_EPOCH

OUTCOME_DECISION_TYPE = "outcome_decision"
OUTCOME_DECISIONS = frozenset({"accept", "reject"})
OPEN_DECISIONS = ("reject", "investigate", "unknown")
KIND_RECEIPT_OUTCOME = "receipt_outcome"
KIND_ACTIVE_DISPATCH = "active_dispatch"
KIND_ABANDONED_DISPATCH = "abandoned_dispatch"

# What an active dispatch without an outcome reads as in the list.
OUTCOME_NO_RECEIPT = "no_receipt"
OUTCOME_NO_OUTCOME = "no_outcome"
# What an abandoned bundle nothing decided on reads as.
OUTCOME_NO_DECISION = "no_decision"

# An active dispatch without any receipt is still running until it is this
# old; after that it is an open point (the drain's --older-than-hours default).
NO_RECEIPT_THRESHOLD_HOURS = 1.0

# The index a reader of receipts/processed builds: receipt_outcome's decision
# read as the drain's status literal. ``unknown`` and ``superseded`` carry no
# result of their own and have no entry.
_INDEX_STATUS = {"accept": "success", "reject": "failure", "investigate": "investigate"}
_STATUS_OUTCOME = {v: k for k, v in _INDEX_STATUS.items()}

_UNSCOPED_PROJECT = "\x00unscoped"

# Noise that is not this dispatch's receipt: a test's line, or another
# project's under a colliding id (ADR-007). A frozen contract_invalid is no
# outcome but it is a receipt the dispatch wrote.
_NOT_A_RECEIPT = frozenset({"pytest", "foreign_project", "temp_report_path", "magicmock"})

# The always-loaded t0_index.json carries at most this many items, the rest as a count.
INDEX_LIMIT = 10

_DECISION_ACTION = {"accept": "approve", "reject": "reject"}


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def iter_complete_lines(path: Path) -> Iterator[Dict[str, Any]]:
    """Yield each JSON object on a complete (newline-terminated) line of ``path``.

    A last line without a newline is a writer mid-append and is skipped; a
    malformed or non-object line is skipped too. A missing file yields nothing.
    """
    if not path.exists():
        return
    with open(path, "rb") as fh:
        for raw in fh:
            if not raw.endswith(b"\n"):
                return
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(obj, dict):
                yield obj


def read_outcome_decisions(log_path: Path, project_id: str) -> Dict[str, Dict[str, Any]]:
    """Map dispatch_id -> the last outcome decision of ``project_id`` in file order.

    An empty ``project_id`` reads nothing: a decision is only ever taken as
    one of a named project (ADR-007).
    """
    decisions: Dict[str, Dict[str, Any]] = {}
    if not project_id:
        return decisions
    for record in iter_complete_lines(Path(log_path)):
        if record.get("decision_type") != OUTCOME_DECISION_TYPE:
            continue
        if record.get("project_id") != project_id:
            continue
        did = record.get("dispatch_id")
        if not isinstance(did, str) or not did.strip():
            continue
        if record.get("decision") not in OUTCOME_DECISIONS:
            continue
        decisions[did.strip()] = record
    return decisions


def record_outcome_decision(
    log_path: Path,
    *,
    dispatch_id: str,
    project_id: str,
    decision: str,
    reason: str = "",
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one outcome decision through ``t0_decision_log.write_decision``.

    Raises ValueError on an empty dispatch_id or project_id or a decision
    outside ``accept``/``reject``. Returns the record written.
    """
    did = (dispatch_id or "").strip()
    pid = (project_id or "").strip()
    if not did:
        raise ValueError("dispatch_id is required")
    if not pid:
        raise ValueError("project_id is required (ADR-007): a decision is always one project's")
    if decision not in OUTCOME_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(OUTCOME_DECISIONS)}, got {decision!r}")

    # imported here: t0_decision_log resolves its default paths at import time
    from t0_decision_log import build_record, write_decision

    record = build_record(
        action=_DECISION_ACTION[decision],
        reasoning=reason,
        dispatch_id=did,
        timestamp=timestamp,
    )
    record["decision_type"] = OUTCOME_DECISION_TYPE
    record["decision"] = decision
    record["project_id"] = pid
    # settled at write time: nothing for t0_decision_reconcile to resolve
    record["outcome_pending"] = False
    write_decision(record, Path(log_path))
    return record


# ---------------------------------------------------------------------------
# The one predicate: has a receipt, and what its outcome is
# ---------------------------------------------------------------------------

def store_project_id(data_dir: Path) -> str:
    """The project whose store ``data_dir`` is: derived from its state dir,
    else ambient (``VNX_PROJECT_ID``, ``.vnx-project-id``), else ""."""
    from vnx_paths import project_id_from_state_dir  # noqa: PLC0415

    derived = project_id_from_state_dir(Path(data_dir) / "state")
    if derived:
        return derived
    try:
        from project_root import resolve_project_id  # noqa: PLC0415
        return resolve_project_id()
    except RuntimeError:
        return ""


def scope_receipts(receipts: List[Dict[str, Any]], project_id: str) -> tuple:
    """``(receipts, project)`` to read them for. Without a project id the
    lines' own ``project_id`` is dropped and they are read for a sentinel
    project, so no line counts as another project's and no decision matches."""
    if project_id:
        return receipts, project_id
    return ([{k: v for k, v in r.items() if k != "project_id"} for r in receipts],
            _UNSCOPED_PROJECT)


def read_processed(receipts_dir: Path) -> List[Dict[str, Any]]:
    """The receipts in ``<receipts_dir>/processed`` in file-name order: the
    names start with the write time in epoch seconds, so this is the order
    they arrived in. Malformed and non-object files are skipped."""
    processed = Path(receipts_dir) / "processed"
    receipts: List[Dict[str, Any]] = []
    if not processed.is_dir():
        return receipts
    for path in sorted(processed.iterdir(), key=lambda p: p.name):
        if path.suffix != ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            receipts.append(data)
    return receipts


def scoped_processed(receipts_dir: Path) -> tuple:
    """The processed receipts of a store and the project they are read for
    (``receipts_dir`` is ``<data dir>/receipts``)."""
    receipts_dir = Path(receipts_dir)
    if not (receipts_dir / "processed").is_dir():
        return [], _UNSCOPED_PROJECT
    return scope_receipts(read_processed(receipts_dir), store_project_id(receipts_dir.parent))


def receipt_owner(receipt: Dict[str, Any], project_id: str) -> str:
    """The dispatch id ``receipt`` is a receipt of in ``project_id``, outcome or
    not, or "" when it is none: bookkeeping is written around a dispatch, not
    by it; test noise and another project's line are no receipt of this one."""
    did = str(receipt.get("dispatch_id") or "").strip()
    if (did.lower() in INVALID_DISPATCH_IDS
            or receipt.get("event_type") in BOOKKEEPING_EVENT_TYPES
            or noise_reason(receipt, project_id) in _NOT_A_RECEIPT):
        return ""
    return did


def receipt_presence(receipts: Iterable[Dict[str, Any]], project_id: str,
                     summary: Dict[str, Any]) -> frozenset:
    """The dispatch ids of ``project_id`` with a receipt, outcome or not: only
    evidence, superseded, attributed to another dispatch, or dropped as an
    unlinked gate run all count. Bookkeeping, test noise and another
    project's lines do not."""
    present = {receipt_owner(r, project_id) for r in receipts if isinstance(r, dict)} - {""}
    # a dispatch that another id's lines were attributed to has a receipt too
    return frozenset(present | {o["dispatch_id"] for o in summary["outcomes"]})


def receipt_status_index(summary: Dict[str, Any]) -> Dict[str, str]:
    """dispatch_id -> ``success`` | ``failure`` | ``investigate``, one per
    dispatch with an outcome of its own."""
    return {o["dispatch_id"]: _INDEX_STATUS[o["decision"]]
            for o in summary["outcomes"] if o["decision"] in _INDEX_STATUS}


def dispatch_receipts(receipts: List[Dict[str, Any]], project_id: str) -> tuple:
    """``(status_index, presence)`` of ``receipts`` for ``project_id``: the one
    reading every mover of dispatch files uses."""
    summary = summarize(receipts, project_id=project_id)
    return receipt_status_index(summary), receipt_presence(receipts, project_id, summary)


_DECISION_DESTINATION = {"accept": "completed", "reject": "dead_letter"}

RECEIPT_WITHOUT_OUTCOME = "receipt present without an outcome: needs a human look"


def active_destination(
    *,
    receipt_status: Optional[str],
    has_receipt: bool,
    decision: Optional[str],
    age_seconds: Optional[float],
    threshold_seconds: float,
) -> tuple:
    """Where a dispatch in ``active/`` goes: ``(destination, reason, open)``.

    ``destination`` is ``completed``, ``dead_letter`` or ``skipped`` (it stays).
    ``open`` is what the open point reads as, or None when it is none (moved,
    or without a receipt and still younger than the threshold).

    A T0 ``decision`` goes first. Only an accept goes to completed; dead_letter
    is only a T0 reject. Everything else stays in active/ as an open point:
    a failure or investigate outcome, a receipt without an outcome of its own,
    and no receipt past the threshold or without a timestamp.
    """
    if decision in _DECISION_DESTINATION:
        return _DECISION_DESTINATION[decision], f"T0 decision {decision!r} in the decision log", None
    if receipt_status == "success":
        return "completed", "receipt found with success status", None
    if receipt_status is not None:
        outcome = _STATUS_OUTCOME.get(receipt_status, receipt_status)
        return "skipped", (f"open point: outcome {outcome!r} needs a human look "
                           "(a T0 decides: receipt_query.py decide)"), outcome
    if has_receipt:
        # Only evidence, superseded, attributed to another id or an unlinked
        # gate run: the dispatch left a receipt, so it is no orphan, and nothing
        # says it failed.
        return "skipped", RECEIPT_WITHOUT_OUTCOME, OUTCOME_NO_OUTCOME
    if age_seconds is None:
        return "skipped", "open point: no receipt, no timestamp", OUTCOME_NO_RECEIPT
    if age_seconds >= threshold_seconds:
        return "skipped", (f"open point: no receipt, age {age_seconds / 3600:.1f}h "
                           "> threshold"), OUTCOME_NO_RECEIPT
    return "skipped", f"no receipt yet, age {age_seconds / 3600:.2f}h < threshold", None


class DispatchEntry(NamedTuple):
    """A dispatch in a live bucket: ``active/``, ``rejected/<reason>/`` or
    ``abandoned/``. ``directory`` is its path there: a ``<id>/`` directory, or
    an ``<id>.md`` file the headless daemon or the post-exit cleanup left
    behind (D4b2). ``bucket`` names which one it is: ``active``, ``rejected``
    or ``abandoned`` (a ``<id>/`` bundle ``dispatch_cleanup.py`` moved there)."""
    dispatch_id: str
    directory: Path
    timestamp: Optional[datetime]
    bucket: str = "active"


class ActiveScan(NamedTuple):
    """``active/`` read once: the dispatches, and ``(name, reason)`` for each
    ``.md`` that is no dispatch and is ignored."""
    entries: List[DispatchEntry]
    ignored: List[tuple]


# The marker every dispatch file carries (headless_dispatch_daemon, the bash
# dispatcher); a README or a note in active/ has none.
_DISPATCH_MARKER = re.compile(r"\[\[TARGET:[^\]\s]+\]\]")

NOT_A_DISPATCH = "not a dispatch: no [[TARGET:...]] marker"


def _manifest_timestamp(raw: str) -> Optional[datetime]:
    """ISO-8601 manifest timestamp as an aware UTC datetime, or None."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return None


def markdown_dispatch(path: Path, bucket: str = "active") -> tuple:
    """``(entry, None)`` when ``active/<id>.md`` is a dispatch, else
    ``(None, reason)``. The id is the file stem, the one the headless daemon
    delivers and its receipts carry; the timestamp is the file's mtime (the
    move into active/ keeps it). A file that cannot be read stays a dispatch:
    nothing proves it is none, and an open point is safer than a silent one."""
    path = Path(path)
    try:
        is_dispatch = bool(_DISPATCH_MARKER.search(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        # vnx-silent-except: unreadable reads as a dispatch (an open point), see above.
        is_dispatch = True
    if not is_dispatch:
        return None, NOT_A_DISPATCH
    try:
        timestamp: Optional[datetime] = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        # vnx-silent-except: without a timestamp the dispatch is an open point.
        timestamp = None
    return DispatchEntry(dispatch_id=path.stem, directory=path, timestamp=timestamp,
                         bucket=bucket), None


def _scan_bucket(bucket_dir: Path, bucket: str = "active") -> ActiveScan:
    """Read one directory of dispatches: every subdirectory and every ``.md``
    that is a dispatch (``markdown_dispatch``). Other files are no entry."""
    entries: List[DispatchEntry] = []
    ignored: List[tuple] = []
    if not bucket_dir.is_dir():
        return ActiveScan(entries, ignored)
    for path in sorted(bucket_dir.iterdir()):
        if path.is_dir():
            entries.append(_directory_dispatch(path, bucket))
        elif path.is_file() and path.suffix == ".md":
            entry, reason = markdown_dispatch(path, bucket)
            if entry is None:
                ignored.append((path.name, reason))
            else:
                entries.append(entry)
    return ActiveScan(entries, ignored)


def scan_active(dispatches_dir: Path) -> ActiveScan:
    """Read ``dispatches/active/``: every directory and every ``.md`` that is a
    dispatch (``markdown_dispatch``). Other files are no entry."""
    return _scan_bucket(Path(dispatches_dir) / "active", "active")


def scan_rejected(dispatches_dir: Path) -> ActiveScan:
    """Read ``dispatches/rejected/<reason>/``: dispatch files the post-worker-exit
    cleanup moved there on a non-success exit, one directory per reason
    (``failure``/``timeout``/``killed``/``stuck``). The reason subdirectory is
    storage, not an outcome: no T0 decided anything by the move, so these stay
    open points exactly like a file in ``active/`` (``scan_active``)."""
    rejected = Path(dispatches_dir) / "rejected"
    entries: List[DispatchEntry] = []
    ignored: List[tuple] = []
    if not rejected.is_dir():
        return ActiveScan(entries, ignored)
    for reason_dir in sorted(rejected.iterdir()):
        if not reason_dir.is_dir():
            continue
        scan = _scan_bucket(reason_dir, "rejected")
        entries.extend(scan.entries)
        ignored.extend(scan.ignored)
    return ActiveScan(entries, ignored)


def scan_open_buckets(dispatches_dir: Path) -> ActiveScan:
    """Every bucket a dispatch file the fabric has not decided on can sit in:
    ``active/`` first, then ``rejected/<reason>/`` (``scan_active`` /
    ``scan_rejected``). One dispatch with a file in both is one entry — the
    ``active/`` one wins in ``active_dispatch_items``'s ``seen`` filter."""
    active = scan_active(dispatches_dir)
    rejected = scan_rejected(dispatches_dir)
    return ActiveScan(active.entries + rejected.entries, active.ignored + rejected.ignored)


def iter_active_dispatches(dispatches_dir: Path) -> Iterator[DispatchEntry]:
    """Yield a DispatchEntry for each dispatch under ``dispatches/active/``:
    a directory, or an ``<id>.md`` of a failed headless delivery."""
    yield from scan_active(dispatches_dir).entries


def _directory_dispatch(entry_dir: Path, bucket: str = "active") -> DispatchEntry:
    """A DispatchEntry for ``active/<id>/``, id and timestamp from its manifest."""
    manifest = entry_dir / "manifest.json"
    dispatch_id = entry_dir.name
    timestamp: Optional[datetime] = None
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            dispatch_id = data.get("dispatch_id", dispatch_id)
            raw_ts = data.get("timestamp", "")
            if raw_ts:
                timestamp = _manifest_timestamp(raw_ts)
        except (json.JSONDecodeError, OSError, AttributeError):
            # vnx-silent-except: an unreadable manifest is a dispatch
            # without a timestamp, which is an open point, never a crash.
            timestamp = None
    return DispatchEntry(dispatch_id=dispatch_id, directory=entry_dir, timestamp=timestamp,
                         bucket=bucket)


def active_dispatch_items(
    data_dir: Path,
    decisions: Dict[str, Dict[str, Any]],
    *,
    project_id: str,
    now: Optional[datetime] = None,
    threshold_hours: float = NO_RECEIPT_THRESHOLD_HOURS,
    scan: Optional[ActiveScan] = None,
) -> List[Dict[str, Any]]:
    """The dispatches in a live bucket (``<data_dir>/dispatches/active/`` and
    ``rejected/<reason>/``) the drain leaves standing as an open point, read
    exactly as the drain reads them: the bucket scan (directories and dispatch
    ``.md`` files), then ``receipts/processed`` through
    ``dispatch_receipts``, then ``active_destination``. ``scan`` is a bucket
    set already read (``scan_open_buckets``); an id in both buckets is one
    item, the ``active/`` entry first."""
    data_dir = Path(data_dir)
    entries = (scan or scan_open_buckets(data_dir / "dispatches")).entries
    if not entries:
        return []
    receipts = read_processed(data_dir / "receipts")
    receipts, pid = scope_receipts(receipts, project_id)
    status_index, presence = dispatch_receipts(receipts, pid)
    now = now or datetime.now(timezone.utc)
    items: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in entries:
        did = entry.dispatch_id
        # one dispatch with files in several buckets (active/, rejected/) is
        # one open point, not two; active/ comes first, so it wins
        if did in seen:
            continue
        seen.add(did)
        age = (now - entry.timestamp).total_seconds() if entry.timestamp else None
        status = status_index.get(did)
        has_receipt = did in presence
        decision = (decisions.get(did) or {}).get("decision")
        _, reason, open_as = active_destination(
            receipt_status=status, has_receipt=has_receipt,
            decision=decision, age_seconds=age,
            threshold_seconds=threshold_hours * 3600.0)
        if (open_as is None and entry.bucket == "rejected"
                and decision is None and status is None and not has_receipt):
            # A file under rejected/<reason>/ is there because the worker
            # exited non-successfully (cleanup_worker_exit step 3), so the
            # "may still be running" grace of the no-receipt threshold does
            # not apply: without a decision it is an open point at once.
            open_as = OUTCOME_NO_RECEIPT
            reason = "open point: no receipt after a failed exit"
        if open_as is None:
            continue
        items.append({
            "kind": KIND_ACTIVE_DISPATCH,
            "dispatch_id": did,
            "outcome": open_as,
            "status": status_index.get(did),
            "reason": reason,
            "last_seen": entry.timestamp.isoformat() if entry.timestamp else None,
        })
    return items


def _abandoned_dispatch(entry_dir: Path) -> DispatchEntry:
    """A DispatchEntry for ``abandoned/<id>/``: the directory name is the
    dispatch id (cleanup moves ``pending/<id>/`` under the same name), the
    timestamp its mtime. A bundle whose mtime cannot be read stays an entry
    without a timestamp — an open point, never a crash."""
    entry_dir = Path(entry_dir)
    try:
        timestamp: Optional[datetime] = datetime.fromtimestamp(entry_dir.stat().st_mtime, tz=timezone.utc)
    except OSError:
        # vnx-silent-except: without a timestamp the bundle is still an open point.
        timestamp = None
    return DispatchEntry(dispatch_id=entry_dir.name, directory=entry_dir, timestamp=timestamp)


def scan_abandoned(dispatches_dir: Path) -> List[DispatchEntry]:
    """Read ``dispatches/abandoned/``: every directory there is a bundle
    ``dispatch_cleanup.py`` moved out of ``pending/``. An entry that cannot be
    read is still a dispatch (``_abandoned_dispatch``): an open point is safer
    than a silent one, so a stat that fails lists the entry rather than dropping it."""
    abandoned = Path(dispatches_dir) / "abandoned"
    entries: List[DispatchEntry] = []
    if not abandoned.is_dir():
        return entries
    for path in sorted(abandoned.iterdir()):
        try:
            is_dir = stat.S_ISDIR(os.stat(path, follow_symlinks=False).st_mode)
        except OSError:
            # vnx-silent-except: cannot tell what it is; an open point is safer.
            is_dir = True
        if is_dir:
            entries.append(_abandoned_dispatch(path))
    return entries


ABANDONED_WITHOUT_DECISION = (
    "abandoned bundle without a T0 decision: an open point "
    "(receipt_query.py decide <dispatch-id> accept|reject)")


def abandoned_dispatch_items(
    data_dir: Path,
    decisions: Dict[str, Dict[str, Any]],
    *,
    scan: Optional[List[DispatchEntry]] = None,
) -> List[Dict[str, Any]]:
    """The bundles in ``<data_dir>/dispatches/abandoned/`` that no outcome
    decision of this project closes.

    ``decisions`` is already scoped to the project (``read_outcome_decisions``):
    another project's decision about the same dispatch id is not in it, so it
    closes nothing. ``scan`` is an ``abandoned/`` already read. An unreadable
    bundle is listed too.
    """
    data_dir = Path(data_dir)
    entries = scan if scan is not None else scan_abandoned(data_dir / "dispatches")
    items: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in entries:
        did = entry.dispatch_id
        # an id both here and in another list is one open point (the caller
        # drops the duplicate); a decision of this project closes it.
        if did in seen or did in decisions:
            continue
        seen.add(did)
        items.append({
            "kind": KIND_ABANDONED_DISPATCH,
            "dispatch_id": did,
            "outcome": OUTCOME_NO_DECISION,
            "status": None,
            "reason": ABANDONED_WITHOUT_DECISION,
            "last_seen": entry.timestamp.isoformat() if entry.timestamp else None,
        })
    return items


def _last_seen(receipts: Iterable[Dict[str, Any]], project_id: str) -> Dict[str, datetime]:
    """Latest receipt timestamp per dispatch_id over the lines ``summarize``
    keeps for ``project_id``: ``noise_reason`` is the one project test, so a
    receipt of another project or a noise line never makes an outcome of this
    one look fresh (ADR-007)."""
    seen: Dict[str, datetime] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or noise_reason(receipt, project_id) is not None:
            continue
        did = str(receipt.get("dispatch_id") or "").strip()
        ts = _parse_ts(receipt.get("timestamp"))
        if did and ts is not None:
            ts = ts.astimezone(timezone.utc)
            if did not in seen or ts > seen[did]:
                seen[did] = ts
    return seen


def open_outcome_items(
    receipts: List[Dict[str, Any]],
    decisions: Dict[str, Dict[str, Any]],
    *,
    project_id: str,
    since: str = OPEN_OUTCOMES_EPOCH,
) -> List[Dict[str, Any]]:
    """Every ``reject``/``investigate``/``unknown`` dispatch of ``project_id``
    without a decision, newest first (by its latest receipt timestamp)."""
    summary = summarize(receipts, project_id=project_id, cutoff=_parse_ts(since))
    last_seen = _last_seen(receipts, project_id)
    items: List[Dict[str, Any]] = []
    for outcome in summary["outcomes"]:
        did = outcome["dispatch_id"]
        if outcome["decision"] not in OPEN_DECISIONS or did in decisions:
            continue
        seen = last_seen.get(did)
        items.append({
            "kind": KIND_RECEIPT_OUTCOME,
            "dispatch_id": did,
            "outcome": outcome["decision"],
            "status": outcome.get("status"),
            "reason": outcome.get("reason"),
            "last_seen": seen.isoformat() if seen else None,
        })
    items.sort(key=lambda i: i["dispatch_id"])
    items.sort(key=lambda i: i["last_seen"] or "", reverse=True)
    return items


def build_open_outcomes(
    state_dir: Path,
    *,
    project_id: str,
    ledger_path: Optional[Path] = None,
    decision_log_path: Optional[Path] = None,
    limit: Optional[int] = INDEX_LIMIT,
    since: str = OPEN_OUTCOMES_EPOCH,
    data_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    no_receipt_threshold_hours: float = NO_RECEIPT_THRESHOLD_HOURS,
) -> Dict[str, Any]:
    """The open-outcomes section: counts over all open points, at most
    ``limit`` items (None = all), and ``more`` for the rest.

    ``data_dir`` (default ``state_dir.parent``) holds ``dispatches/active``,
    ``dispatches/rejected/<reason>`` and ``receipts/processed`` for the
    ``active_dispatch`` kind, and ``dispatches/abandoned`` for the
    ``abandoned_dispatch`` kind. A dispatch the ledger already lists is listed
    once, as ``receipt_outcome``; one with files in several buckets is one
    item too.

    Stateless: two readers at the same moment get the same answer, and
    reading consumes nothing. Without a project id, or when a file cannot be
    read, the section is ``available: false`` with a reason — never an
    empty list that reads as "nothing open".
    """
    state_dir = Path(state_dir)
    if not project_id:
        return {"available": False, "reason": "no project_id: open outcomes are read per project (ADR-007)"}
    ledger = Path(ledger_path) if ledger_path else state_dir / LEDGER_NAME
    log = Path(decision_log_path) if decision_log_path else state_dir / DECISION_LOG_NAME
    try:
        receipts = list(iter_complete_lines(ledger))
        decisions = read_outcome_decisions(log, project_id)
    except OSError as exc:
        return {"available": False, "reason": f"could not read {ledger.name} or {log.name}: {exc}"}

    items = open_outcome_items(receipts, decisions, project_id=project_id, since=since)
    listed = {i["dispatch_id"] for i in items}
    store = Path(data_dir) if data_dir else state_dir.parent
    try:
        scan = scan_open_buckets(store / "dispatches")
        active = active_dispatch_items(
            store, decisions, project_id=project_id, now=now,
            threshold_hours=no_receipt_threshold_hours, scan=scan)
        abandoned = abandoned_dispatch_items(store, decisions)
    except OSError as exc:
        return {"available": False, "reason": f"could not read dispatches/active, /rejected or /abandoned: {exc}"}
    # an id in more than one bucket is one open point: the ledger kind wins,
    # then active/ (over rejected/), then the abandoned bucket.
    for item in active + abandoned:
        if item["dispatch_id"] in listed:
            continue
        listed.add(item["dispatch_id"])
        items.append(item)
    items.sort(key=lambda i: i["dispatch_id"])
    items.sort(key=lambda i: i["last_seen"] or "", reverse=True)
    shown = items if limit is None else items[:max(0, limit)]
    by_outcome = {d: 0 for d in OPEN_DECISIONS}
    by_kind: Dict[str, int] = {}
    for item in items:
        by_outcome[item["outcome"]] = by_outcome.get(item["outcome"], 0) + 1
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return {
        "available": True,
        "project_id": project_id,
        "since": since,
        "total": len(items),
        "by_outcome": by_outcome,
        "by_kind": by_kind,
        "items": shown,
        "more": len(items) - len(shown),
        # .md files in active/ and rejected/<reason>/ that are no dispatch
        # (NOT_A_DISPATCH); the drain names the active/ ones
        "ignored": len(scan.ignored),
    }
