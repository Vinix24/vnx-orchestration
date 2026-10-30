#!/usr/bin/env python3
"""cleanup_orphan_gates.py — resolve gate requests older than 24h with no matching result.

An orphan gate request is a ``review_gates/requests/`` record with no
corresponding ``results/`` record. Ending it is a gate outcome, so it goes
through the one gate writer, ``gate_outcome.commit_gate_outcome``: under the
slot's lock that writes one result record, books its one
``review_gate_result`` receipt and ends the request. Nothing here writes a gate
record itself (ADR-046 §2), and ``tests/test_oi1472_residual_guard_writers.py``
lists this module in its writer guard.

The orphan becomes ``unavailable`` — there is no ``abandoned`` outcome
(ADR-046 §1) — with the reason ``abandoned_orphan_request``, who ended it, the
age in hours and the source request stem. The request ends ``unavailable``
through ``gate_outcome.REQUEST_STATUS_OF``, so ``execute_gate`` short-circuits
on it and a real retry is a new request.

**The scan is not the commit** (GE-8 round 2). A scan reads a request, and a
retry can replace it in the same ``(gate, PR)`` slot before the commit lands.
The commit therefore holds the result slot's lock AND the request's lock
(:func:`atomic_io.slot_lock` is reentrant inside one process, so the request
lock the writer's step 7 takes again is the same critical section), re-reads
the request there and compares its identity — ``dispatch_id``, ``commit_sha``,
``requested_at``, ``pr_number``, ``pr_id`` — and its age with what the scan
saw. Anything different is no longer the orphan this run found: no record, no
request end, and the orphan is reported as ``skipped_replaced``. The request
written is the one read under the lock, so nothing the scan saw can overwrite
fresher metadata.

**A half-ended request is finished, not re-resolved** (GE-8 round 2). Step 7's
write can fail after the record and its receipt landed, which leaves a request
still ``executing`` while its slot holds the cleanup's ``unavailable`` record;
the next scan used to skip it (the slot has a result) and the request stayed in
flight for ever. The scan now also finds that state and finishes the SAME
request through the same writer: the record on the slot is handed back
unchanged, which step 6 reads as a repeat, so no second record and no second
receipt is written and only the request's end lands.

**The record names the request's identity, and an unpaid receipt is recovery's
work too** (GE-8 round 4). ``dispatch_id`` and ``commit_sha`` sit on the
record's top level as well as inside ``original_request``, because the booked
``review_gate_result`` is deduplicated on the receipt's fields, not on a nested
copy: without them the key fell back to the emission timestamp and a retry
after a crash appended a second line for the same attempt. And a request that
has ended while its record still carries ``receipt_pending: true`` is picked up
by the recovery scan: the append failed, the orphan scan drops the slot (it has
a result) and the ended request used to drop it too, so the debt was never
paid. The recovery pays it through the same stable identity.

The store is resolved through ``project_root.resolve_data_dir`` (ADR-026,
issue #225): ``$VNX_DATA_DIR`` needs ``VNX_DATA_DIR_EXPLICIT=1``; never a
repo-relative ``.vnx-data`` fallback.

Usage: ``python3 scripts/cleanup_orphan_gates.py [--dry-run]``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import gate_recorder  # noqa: E402
from atomic_io import slot_lock  # noqa: E402
from gate_outcome import (  # noqa: E402
    GateOutcome, GateRequest, RequestWriteFailed, commit_gate_outcome,
)
from gate_status import REQUEST_TERMINAL_STATUSES, canonical_status  # noqa: E402
from project_root import resolve_data_dir  # noqa: E402

ABANDONED_ORPHAN_REQUEST = "abandoned_orphan_request"
ABANDONED_BY = "cleanup_orphan_gates"

# What one request's resolution did. ``skipped_replaced`` is not a write
# failure: the slot changed under the scan and this run must leave it alone.
RESOLVED = "resolved"
ALREADY_RESOLVED = "already_resolved"
SKIPPED_REPLACED = "skipped_replaced"
DRY_RUN = "dry_run"
FAILED = "failed"

# The fields that identify ONE request, compared between the scan's copy and the
# one read under the lock: what a retry in the same slot changes first. A field
# only one side names identifies nothing and cannot be evidence of a match.
_REQUEST_IDENTITY_FIELDS: Tuple[str, ...] = (
    "dispatch_id", "commit_sha", "requested_at", "pr_number", "pr_id",
)

# ``main`` points these at the CLI overrides; otherwise the resolver decides.
_REQUESTS_DIR: Optional[Path] = None
_RESULTS_DIR: Optional[Path] = None


class OrphanResolution(NamedTuple):
    """What one resolution did; ``detail`` says why.

    ``status`` is the name the cleanup summary counts. Truthiness is "the run
    must not report a write failure": a resolved orphan, one another writer
    already resolved, one skipped because its slot was replaced, and a dry run
    are all answers; only :data:`FAILED` is not.
    """

    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAILED

    def __bool__(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



def _gate_dirs() -> Tuple[Path, Path]:
    """The store's pair from the central resolver — never a repo-relative fallback."""
    review_gates = resolve_data_dir(__file__) / "state" / "review_gates"
    return review_gates / "requests", review_gates / "results"


def _current_dirs() -> Tuple[Path, Path]:
    """The live pair: ``main``'s CLI override first, else the resolver's."""
    if _REQUESTS_DIR is None or _RESULTS_DIR is None:
        return _gate_dirs()
    return _REQUESTS_DIR, _RESULTS_DIR


def _file_age_hours(path: Path) -> float:
    """Return age of file in hours."""
    try:
        mtime = path.stat().st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except OSError:
        return 0.0


def _read_request(path: Path) -> Dict[str, Any]:
    """Parse request file. Return empty dict on failure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_pr_number_from_stem(stem: str) -> Optional[int]:
    """Extract PR number from gate request stem like 'pr-57-gemini_review'."""
    match = re.match(r"^pr-(\d+)-", stem)
    return int(match.group(1)) if match else None


def _slot_identity(stem: str, request_data: Dict[str, Any]) -> Tuple[str, Optional[int], str]:
    """The ``(gate, pr_number, pr_id)`` that reproduce this request file's own name.

    The filename is authoritative: the request must be ENDED at the path it was
    found at, and ``persist_request`` re-derives that path from these values. A
    payload ``pr_id`` on a ``pr-<N>-<gate>.json`` request names another slot.
    """
    if stem.endswith("-contract"):
        base = stem[: -len("-contract")]
        pr_slug, sep, gate = base.rpartition("-")
        if sep and gate:
            return gate, None, pr_slug
    number, sep, gate = stem[3:].partition("-") if stem.startswith("pr-") else ("", "", "")
    if sep and gate and number.isdigit():
        return gate, int(number), ""
    # A non-standard stem: fall back to what the payload names.
    gate = request_data.get("gate")
    gate_name = gate.strip() if isinstance(gate, str) and gate.strip() else stem
    pr_number = request_data.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        pr_number = _extract_pr_number_from_stem(stem)
    pr_id = request_data.get("pr_id")
    return gate_name, pr_number, pr_id.strip() if isinstance(pr_id, str) else ""


def _find_orphans(max_age_hours: float = 24.0) -> List[Dict[str, Any]]:
    """Orphans: request files with no result, older than ``max_age_hours`` (dicts)."""
    requests_dir, results_dir = _current_dirs()
    if not requests_dir.exists():
        return []

    orphans: List[Dict[str, Any]] = []
    for req_path in sorted(requests_dir.glob("*.json")):
        result_path = results_dir / req_path.name
        if result_path.exists():
            continue  # already has a result
        age = _file_age_hours(req_path)
        if age < max_age_hours:
            continue  # too recent

        stem = req_path.stem
        request_data = _read_request(req_path)
        gate_name, pr_number, pr_id = _slot_identity(stem, request_data)
        orphans.append({
            "request_path": req_path,
            "result_path": result_path,
            "gate_name": gate_name,
            "pr_number": pr_number,
            "pr_id": pr_id,
            "stem": stem,
            "age_hours": round(age, 1),
            "request_data": request_data,
        })

    return orphans


def _named(value: Any) -> bool:
    """Positive evidence: a value that names something — not ``None``, not an empty or blank string."""
    if value is None:
        return False
    return not (isinstance(value, str) and not value.strip())


# The request fields the booked receipt's dedup key reads
# (``append_receipt_internals.idempotency.IDEMPOTENCY_FIELDS``) and the cleanup
# record therefore carries at its TOP level (GE-8 round 4). Leaving them nested
# in ``original_request`` gave the receipt no stable identity: the key fell back
# to the emission timestamp, so a retry after a crash between the append and
# clearing ``receipt_pending`` appended a SECOND ``review_gate_result`` for the
# same attempt.
_OUTCOME_IDENTITY_FIELDS: Tuple[str, ...] = ("dispatch_id", "commit_sha")


def _request_identity(request_data: Dict[str, Any]) -> Dict[str, Any]:
    """The request's identity for the record's top level; a value it does not name is not invented."""
    return {name: request_data[name] for name in _OUTCOME_IDENTITY_FIELDS if _named(request_data.get(name))}


def _identity_mismatch(scan_payload: Dict[str, Any], on_disk: Dict[str, Any]) -> Optional[str]:
    """The first :data:`_REQUEST_IDENTITY_FIELDS` the scan and the slot disagree on, or ``None``.

    Positive evidence only, the way ``gate_outcome`` decides a foreign request:
    a field neither side names identifies nothing and decides nothing; a field
    only ONE side names, or two different values, means the slot no longer
    holds the request this run scanned.
    """
    for name in _REQUEST_IDENTITY_FIELDS:
        mine, theirs = scan_payload.get(name), on_disk.get(name)
        if not (_named(mine) or _named(theirs)):
            continue
        if not (_named(mine) and _named(theirs)) or mine != theirs:
            return f"{name!r} differs ({theirs!r} on the slot, {mine!r} at the scan)"
    return None


def _stale_reason(orphan: Dict[str, Any], on_disk: gate_recorder.RequestOnDisk, max_age_hours: float) -> Optional[str]:
    """Why the request on the slot is no longer the orphan the scan found, or ``None``.

    Called under the request's lock, so the answer is about the state the commit
    is about to write into. Absent and unreadable are NOT "nothing to overwrite"
    but "cannot be shown to be the scan's request", and they skip like a
    mismatch: this run must never end a request it cannot identify.
    """
    if not on_disk.present:
        return f"no request at {on_disk.path} any more"
    if on_disk.record is None:
        return f"the request at {on_disk.path} cannot be read"
    scan_payload = orphan.get("request_data") if isinstance(orphan.get("request_data"), dict) else {}
    mismatch = _identity_mismatch(scan_payload, on_disk.record)
    if mismatch:
        return mismatch
    age_hours = _file_age_hours(on_disk.path)
    if age_hours < max_age_hours:
        return f"age {age_hours:.1f}h is under the {max_age_hours:.1f}h threshold"
    return None


def _preview(orphan: Dict[str, Any], request_path: Optional[Path], max_age_hours: float) -> OrphanResolution:
    """A dry run's answer: what the commit WOULD do, from an unlocked read (it writes nothing)."""
    if request_path is None:
        return OrphanResolution(DRY_RUN)
    stale = _stale_reason(orphan, gate_recorder.read_request_record(request_path), max_age_hours)
    return OrphanResolution(SKIPPED_REPLACED, stale) if stale else OrphanResolution(DRY_RUN)


def _commit_orphan(
    orphan: Dict[str, Any], request_data: Dict[str, Any], requests_dir: Path, request_path: Optional[Path],
) -> OrphanResolution:
    """Book the orphan's ``unavailable`` outcome; the caller holds the result and request locks.

    ``request_data`` is the request as read UNDER the lock, so the ended request
    carries the slot's own payload and never a scan-time copy.
    """
    gate_name, pr_number, pr_id = _slot_identity(orphan["stem"], request_data)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "gate": gate_name,
        "pr_id": pr_id,
        "pr_number": pr_number,
        "status": "unavailable",
        "reason": ABANDONED_ORPHAN_REQUEST,
        "abandoned_by": ABANDONED_BY,
        "age_hours": orphan["age_hours"],
        "source_request": orphan["stem"],
        # Top level, not only inside ``original_request``: the receipt's dedup
        # key reads these there, and without them it keys on the emission
        # timestamp (GE-8 round 4).
        **_request_identity(request_data),
        "original_request": request_data,
    }
    # ``pr_id`` stays what the filename names: a filled-in ``str(pr_number)``
    # makes ``persist_request`` end a contract slot, not this orphan's own file.
    outcome = GateOutcome(
        gate=gate_name, status="unavailable", pr_id=pr_id, pr_number=pr_number, record=record,
    )
    # A slot that already holds a record is not ours to end twice: the writer
    # refuses the record, and ending the request is that slot's writer's job.
    request = None
    if request_path is not None and not orphan["result_path"].exists():
        request = GateRequest(
            requests_dir=requests_dir,
            payload=dict(request_data),
            fields={"reason": ABANDONED_ORPHAN_REQUEST, "abandoned_by": ABANDONED_BY},
        )
    try:
        _payload, written = commit_gate_outcome(
            outcome,
            results_dir=orphan["result_path"].parent,
            result_path=orphan["result_path"],
            request=request,
            book_undecided_receipt=True,
        )
    except (OSError, RequestWriteFailed) as exc:
        print(f"  [ERROR] Failed to write result for {orphan['stem']}: {exc}", file=sys.stderr)
        return OrphanResolution(FAILED, f"{type(exc).__name__}: {exc}")
    if not written:
        print(f"  {orphan['stem']}  already resolved: {orphan['result_path'].name} exists")
        return OrphanResolution(ALREADY_RESOLVED, f"{orphan['result_path'].name} exists")
    return OrphanResolution(RESOLVED)


def _write_abandoned_result(
    orphan: Dict[str, Any], dry_run: bool = False, requests_dir: Optional[Path] = None,
    *, max_age_hours: float = 24.0,
) -> OrphanResolution:
    """Book the orphan's ``unavailable`` outcome through the one gate writer.

    The request is re-read and re-checked UNDER its own lock, inside the result
    slot's lock, so the identity check and the write are one critical section
    against a retry landing in the same slot. A request that changed (or is no
    longer old enough to be an orphan) is reported as ``skipped_replaced`` and
    nothing is written for it — no record, no request end.
    """
    request_data = orphan.get("request_data") if isinstance(orphan.get("request_data"), dict) else {}
    # The file name is authoritative: the request must be ENDED at the path the
    # orphan was found at, and ``persist_request`` re-derives that path from these.
    gate_name, pr_number, pr_id = _slot_identity(orphan["stem"], request_data)
    requests_dir = requests_dir or _current_dirs()[0]
    request_path = gate_recorder.result_file_path(requests_dir, gate_name, pr_number, pr_id)

    if dry_run:
        return _preview(orphan, request_path, max_age_hours)
    if request_path is None:
        # No request path to end: the record is all this slot can be given, as before.
        return _commit_orphan(orphan, request_data, requests_dir, request_path=None)
    try:
        with slot_lock(orphan["result_path"]), slot_lock(request_path):
            on_disk = gate_recorder.read_request_record(request_path)
            stale = _stale_reason(orphan, on_disk, max_age_hours)
            if stale:
                print(f"  {orphan['stem']}  skipped_replaced: {stale}")
                return OrphanResolution(SKIPPED_REPLACED, stale)
            return _commit_orphan(orphan, dict(on_disk.record or {}), requests_dir, request_path)
    except OSError as exc:
        print(f"  [ERROR] Failed to take the slot lock for {orphan['stem']}: {exc}", file=sys.stderr)
        return OrphanResolution(FAILED, f"{type(exc).__name__}: {exc}")


def _is_cleanup_record(record: Dict[str, Any]) -> bool:
    """Is this result record the cleanup's own ``unavailable`` booking?"""
    return (
        canonical_status(record) == "unavailable"
        and record.get("reason") == ABANDONED_ORPHAN_REQUEST
        and record.get("abandoned_by") == ABANDONED_BY
    )


def _owes_receipt(record: Dict[str, Any]) -> bool:
    """Does this record still owe its ``review_gate_result`` receipt?

    Only ``receipt_pending is True`` is a debt: that is the one value
    ``gate_outcome`` sets (``_write_record_then_receipt``), and it is the one
    value ``_pay_outstanding_receipt`` pays. A missing, ``None`` or other value
    was not set by this writer and names no debt.
    """
    return record.get("receipt_pending") is True


def _find_unfinished() -> List[Dict[str, Any]]:
    """Work a previous run left behind although the cleanup's record landed (dicts).

    Two shapes, both invisible to :func:`_find_orphans` because the slot holds
    a result:

    - the request is still ``executing``: step 7's write failed after the
      record and its receipt landed, and the request would stay in flight for
      ever;
    - the request has ended while its record still carries
      ``receipt_pending: true``: the receipt append failed, and every other
      scan drops the slot — the orphan scan because it has a result, the
      terminal check here because the request ended (GE-8 round 4). The debt
      is the work, so it is picked up too.
    """
    requests_dir, results_dir = _current_dirs()
    if not requests_dir.exists():
        return []

    unfinished: List[Dict[str, Any]] = []
    for req_path in sorted(requests_dir.glob("*.json")):
        result_path = results_dir / req_path.name
        if not result_path.exists():
            continue
        record = _read_request(result_path)
        if not _is_cleanup_record(record):
            continue
        stem = req_path.stem
        request_data = _read_request(req_path)
        request_ended = canonical_status(request_data) in REQUEST_TERMINAL_STATUSES
        receipt_debt = _owes_receipt(record)
        if request_ended and not receipt_debt:
            continue  # ended and owes nothing: nothing left to finish
        gate_name, pr_number, pr_id = _slot_identity(stem, request_data)
        unfinished.append({
            "request_path": req_path,
            "result_path": result_path,
            "gate_name": gate_name,
            "pr_number": pr_number,
            "pr_id": pr_id,
            "stem": stem,
            "age_hours": round(_file_age_hours(req_path), 1),
            "request_data": request_data,
            "record": record,
            "recovery": True,
            "receipt_debt": receipt_debt,
        })
    return unfinished


def _finish_unfinished(candidate: Dict[str, Any], requests_dir: Path) -> OrphanResolution:
    """Finish the ONE request a half-ended cleanup left ``executing`` (GE-8 round 2).

    The record on the slot is handed back to the writer unchanged: step 6 reads
    it as a repeat, so no second record and no second receipt is written and
    only the request's end lands. The identity check is the same one the
    cleanup does — a request a retry replaced is somebody else's, and it is
    left alone.

    GE-8 round 4: the same hand-back also pays a ``receipt_pending`` debt. The
    writer pays it before step 6, through the same stable receipt identity, so
    a receipt that already landed comes back as a duplicate — never a second
    line. A request that already ended is not written again.
    """
    gate_name, pr_number, pr_id = candidate["gate_name"], candidate["pr_number"], candidate["pr_id"]
    request_path = gate_recorder.result_file_path(requests_dir, gate_name, pr_number, pr_id)
    if request_path is None:
        return OrphanResolution(SKIPPED_REPLACED, "no request path can be derived for this slot")
    try:
        with slot_lock(candidate["result_path"]), slot_lock(request_path):
            return _finish_unfinished_locked(candidate, requests_dir, request_path)
    except OSError as exc:
        print(f"  [ERROR] Failed to take the slot lock for {candidate['stem']}: {exc}", file=sys.stderr)
        return OrphanResolution(FAILED, f"{type(exc).__name__}: {exc}")


def _finish_unfinished_locked(
    candidate: Dict[str, Any], requests_dir: Path, request_path: Path,
) -> OrphanResolution:
    """The recovery's critical section; the caller holds the result and request locks."""
    stem = candidate["stem"]
    on_disk = gate_recorder.read_request_record(request_path)
    if not on_disk.present or on_disk.record is None:
        return OrphanResolution(SKIPPED_REPLACED, f"the request at {request_path} cannot be read any more")
    stored = _read_request(candidate["result_path"])
    if not _is_cleanup_record(stored):
        print(f"  {stem}  already resolved: the slot no longer holds the cleanup's record")
        return OrphanResolution(ALREADY_RESOLVED, "the slot no longer holds the cleanup's record")
    original = stored.get("original_request") if isinstance(stored.get("original_request"), dict) else {}
    if not original:
        # Without the payload the record was booked for there is nothing to
        # match the request against: finishing it would be a guess.
        return OrphanResolution(SKIPPED_REPLACED, "the record names no original_request to match the request against")
    mismatch = _identity_mismatch(original, on_disk.record)
    if mismatch:
        print(f"  {stem}  skipped_replaced: {mismatch}")
        return OrphanResolution(SKIPPED_REPLACED, mismatch)

    outcome = GateOutcome(
        gate=candidate["gate_name"], status="unavailable", pr_id=candidate["pr_id"],
        pr_number=candidate["pr_number"], record=dict(stored),
    )
    # An already-ended request has nothing left to write: this commit is here
    # for the ``review_gate_result`` its record still owes (GE-8 round 4).
    # Handing the writer no request keeps a finished request byte for byte.
    request = None
    if canonical_status(on_disk.record) not in REQUEST_TERMINAL_STATUSES:
        request = GateRequest(
            requests_dir=requests_dir,
            payload=dict(on_disk.record),
            fields={"reason": ABANDONED_ORPHAN_REQUEST, "abandoned_by": ABANDONED_BY},
        )
    try:
        _payload, written = commit_gate_outcome(
            outcome,
            results_dir=candidate["result_path"].parent,
            result_path=candidate["result_path"],
            request=request,
            book_undecided_receipt=True,
        )
    except (OSError, RequestWriteFailed) as exc:
        print(f"  [ERROR] Failed to finish the request for {stem}: {exc}", file=sys.stderr)
        return OrphanResolution(FAILED, f"{type(exc).__name__}: {exc}")
    if not written:
        print(f"  [ERROR] The writer refused the recovery commit for {stem}", file=sys.stderr)
        return OrphanResolution(FAILED, "the writer refused the recovery commit")
    return OrphanResolution(RESOLVED)


def _recovery_action(candidate: Dict[str, Any], dry_run: bool) -> str:
    """The recovery's progress line: a receipt debt is not a request end (GE-8 round 4)."""
    if candidate.get("receipt_debt"):
        return ("would book " if dry_run else "booking ") + "the receipt its unavailable record still owes"
    return (
        ("would finish " if dry_run else "finishing ")
        + "the request (its unavailable record is already booked)"
    )


def _log_to_audit(orphans: List[Dict[str, Any]]) -> None:
    """Append cleanup event to governance_audit.ndjson."""
    try:
        from governance_audit import log_enforcement  # noqa: PLC0415

        for orphan in orphans:
            pr_number = _extract_pr_number_from_stem(orphan["stem"])
            dispatch_id = orphan.get("request_data", {}).get("dispatch_id") or None
            recovered = bool(orphan.get("recovery"))
            receipt_debt = bool(orphan.get("receipt_debt"))
            context: dict = {
                "stem": orphan["stem"],
                "age_hours": orphan["age_hours"],
                "gate_name": orphan["gate_name"],
                "recovered": recovered,
            }
            if pr_number is not None:
                context["pr_number"] = pr_number
            if dispatch_id is not None:
                context["dispatch_id"] = dispatch_id
            if receipt_debt:
                context["receipt_debt"] = True
            message = (
                f"Orphan gate {orphan['stem']} receipt booked: the request had ended while its "
                f"unavailable record still owed a review_gate_result"
                if receipt_debt else
                f"Orphan gate {orphan['stem']} request finished after a failed write: its "
                f"unavailable record was already booked"
                if recovered else
                f"Orphan gate {orphan['stem']} abandoned after {orphan['age_hours']:.1f}h with no result"
            )
            log_enforcement(
                check_name="orphan_gate_cleanup",
                level=1,
                result=True,
                context=context,
                message=message,
                dispatch_id=dispatch_id,
            )
    except Exception as exc:  # vnx-broad-except: audit write is best-effort; the cleanup already landed
        print(f"  [WARN] Could not write to governance_audit: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve orphan gate requests with no result (>24h old).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    parser.add_argument("--max-age-hours", type=float, default=24.0,
                        help="Minimum age in hours to consider a request orphaned (default: 24).")
    parser.add_argument("--requests-dir", default=None, help="Override gate requests directory path.")
    parser.add_argument("--results-dir", default=None, help="Override gate results directory path.")
    args = parser.parse_args(argv)

    global _REQUESTS_DIR, _RESULTS_DIR
    _REQUESTS_DIR, _RESULTS_DIR = _gate_dirs()
    if args.requests_dir:
        _REQUESTS_DIR = Path(args.requests_dir)
    if args.results_dir:
        _RESULTS_DIR = Path(args.results_dir)

    orphans = _find_orphans(max_age_hours=args.max_age_hours)
    unfinished = _find_unfinished()

    if not orphans and not unfinished:
        print(f"No orphan gate requests found (threshold: {args.max_age_hours:.0f}h).")
        return 0

    mode_label = "[DRY RUN] " if args.dry_run else ""
    if orphans:
        print(f"{mode_label}Found {len(orphans)} orphan gate request(s):\n")

    resolved_orphans: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for orphan in orphans:
        status = "would book" if args.dry_run else "booking"
        print(
            f"  {orphan['stem']}  age={orphan['age_hours']:.1f}h  "
            f"gate={orphan['gate_name']}  → {status} unavailable outcome"
        )
        resolution = _write_abandoned_result(orphan, dry_run=args.dry_run, max_age_hours=args.max_age_hours)
        counts[resolution.status] = counts.get(resolution.status, 0) + 1
        if resolution.status == RESOLVED:
            resolved_orphans.append(orphan)

    if unfinished:
        print(f"\n{mode_label}Found {len(unfinished)} half-ended request(s) to finish:\n")
    requests_dir = _current_dirs()[0]
    for candidate in unfinished:
        print(
            f"  {candidate['stem']}  gate={candidate['gate_name']}  → "
            f"{_recovery_action(candidate, args.dry_run)}"
        )
        if args.dry_run:
            resolution = OrphanResolution(DRY_RUN)
        else:
            resolution = _finish_unfinished(candidate, requests_dir)
        counts[resolution.status] = counts.get(resolution.status, 0) + 1
        if resolution.status == RESOLVED:
            resolved_orphans.append(candidate)

    if not args.dry_run and resolved_orphans:
        _log_to_audit(resolved_orphans)
        print(f"\nResolved {len(resolved_orphans)} orphan(s). Logged to governance_audit.ndjson.")

    print(
        f"\nCleanup summary: {counts.get(RESOLVED, 0)} resolved, "
        f"{counts.get(ALREADY_RESOLVED, 0)} already_resolved, "
        f"{counts.get(SKIPPED_REPLACED, 0)} skipped_replaced, "
        f"{counts.get(FAILED, 0)} failed"
        + (f", {counts[DRY_RUN]} would resolve (dry run)" if DRY_RUN in counts else "")
    )

    failed = counts.get(FAILED, 0)
    if failed:
        print(f"\n[WARN] {failed} write failure(s) — check stderr.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
