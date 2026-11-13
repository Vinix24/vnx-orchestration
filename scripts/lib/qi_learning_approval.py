#!/usr/bin/env python3
"""The approval record the learning loop's persist path requires (ADR-068, besluit 18).

``<state-dir>/learning_approvals/learn-<changes_sha256>.json`` binds one operator decision to one
change set. ``VNX_LEARNING_LOOP_APPROVED`` only selects the file; the hash is recomputed from the rows
about to be written. Used once, under a lock, before the first DB write, ledger first (ADR-005): a
``learning_persist`` line with the record hash goes to ``intelligence_usage.ndjson``, then the record
gets ``consumed_at``. The ledger wins: a proposal that line names is used, whatever its record says.
Tamper-evident, not tamper-proof: ``write_record`` logs a ``learning_approval`` event with the record
hash first, so a receipt whose record hash no approval event carries points at a forged record.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Collection, Dict, Iterable, List, NoReturn, Tuple

from atomic_io import atomic_write_json, slot_lock
from vnx_paths import refuse_real_central_store_write_under_test_runner

APPROVED_ENV = "VNX_LEARNING_LOOP_APPROVED"
RECORD_DIRNAME = "learning_approvals"
USAGE_LOG_NAME = "intelligence_usage.ndjson"
TORN_TAIL_MARKER = "torn_tail_closed"
RECORD_FIELDS = ("proposal_id", "changes_sha256", "approved_by", "approved_at", "reason", "consumed_at")
_PROPOSAL_ID = re.compile(r"learn-([0-9a-f]{64})")
# The write instant, not learned content: a recomputation at persist time
# carries another clock value than the preview the operator approved.
_WRITE_CLOCK_FIELDS = frozenset({"first_seen", "last_used", "last_seen", "valid_from"})


class LearningApprovalRefused(RuntimeError):
    """Raised before any DB write: the reason is in the message."""


def _refuse(reason: str) -> NoReturn:
    raise LearningApprovalRefused(f"{reason}; nothing was written")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def preview(changes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """The rows as the hash sees them (clock fields dropped, order fixed), their hash and the id to approve."""
    rows = sorted(({k: v for k, v in row.items() if k not in _WRITE_CLOCK_FIELDS} for row in changes), key=_canonical)
    digest = _sha256(rows)
    return {"proposal_id": f"learn-{digest}", "changes_sha256": digest, "changes": rows}


def record_path(state_dir: Path, proposal_id: str) -> Path:
    return Path(state_dir) / RECORD_DIRNAME / f"{proposal_id}.json"


def _digest_of(proposal_id: Any) -> str:
    match = _PROPOSAL_ID.fullmatch(proposal_id) if isinstance(proposal_id, str) else None
    if match is None:
        _refuse(f"{proposal_id!r} is no proposal id (learn-<64 hex> from the shadow preview)")
    return match.group(1)


def _parsed(parse: Callable[[Any], Any], value: Any) -> Any:
    """``parse(value)``, or None when ``value`` does not parse."""
    try:
        return parse(value)
    except (TypeError, ValueError):
        return None


def load_approval(state_dir: Path, valid_approvers: Collection[str]) -> Dict[str, Any]:
    """The unused record ``VNX_LEARNING_LOOP_APPROVED`` selects, or a refusal naming why. Reads only."""
    proposal_id = os.environ.get(APPROVED_ENV)
    if not proposal_id:
        _refuse(f"{APPROVED_ENV} is not set: persist needs an approval record (ADR-068 besluit 18)")
    digest = _digest_of(proposal_id)
    path = record_path(state_dir, proposal_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _refuse(f"no approval record at {path}")
    except (OSError, ValueError) as exc:
        _refuse(f"approval record {path} is unreadable or no JSON ({exc})")
    if not isinstance(record, dict) or set(record) != set(RECORD_FIELDS):
        _refuse(f"approval record {path} does not carry exactly the fields {list(RECORD_FIELDS)}")
    if record["proposal_id"] != proposal_id or record["changes_sha256"] != digest:
        _refuse(f"approval record {path} belongs to another proposal")
    if record["consumed_at"] is not None:
        _refuse(f"approval record {path} was already used ({record['consumed_at']!r})")
    if _claimed_in_log(state_dir, proposal_id):
        _refuse(f"approval {proposal_id} was already used: {USAGE_LOG_NAME} holds its learning_persist line")
    if not isinstance(record["approved_by"], str) or record["approved_by"] not in valid_approvers:
        _refuse(f"approved_by {record['approved_by']!r} is not one of {sorted(valid_approvers)}")
    approved_at = _parsed(datetime.fromisoformat, record["approved_at"])
    if not isinstance(record["reason"], str) or not record["reason"].strip() or approved_at is None:
        _refuse(f"approval record {path} has no reason or no ISO approved_at")
    return record


def _torn_tail_marker(offset: int, fragment: bytes) -> Dict[str, Any]:
    return {"event_type": TORN_TAIL_MARKER, "offset": offset, "length": len(fragment),
            "sha256": hashlib.sha256(fragment).hexdigest()}


def _closes(marker: Dict[str, Any], offset: int, line: bytes) -> bool:
    """Whether ``marker`` is the line ``append_usage_event`` writes after a torn ``line`` (no ``\\n``) at ``offset``.
    Integers only: ``19.0 == 19`` in Python, and the writer never writes a float or a bool there."""
    return (bool(line) and marker == _torn_tail_marker(offset, line)
            and all(type(marker[key]) is int for key in ("offset", "length")))


def _untrusted(log: Path, number: int, why: str) -> NoReturn:
    _refuse(f"{log} line {number} {why}: the log is not trustworthy")


def _claimed_in_lines(log: Path, lines: Iterable[bytes], proposal_id: str) -> bool:
    claimed, start, before, unclosed, raw = False, 0, (0, b""), None, b"\n"
    for number, raw in enumerate(lines, 1):
        event = _parsed(json.loads, raw)
        persist = isinstance(event, dict) and event.get("event_type") == "learning_persist"
        if isinstance(event, dict) and event.get("event_type") == TORN_TAIL_MARKER:
            if not _closes(event, *before):
                _untrusted(log, number, "is a torn_tail_closed marker that does not match the line before it")
            unclosed = None
        elif unclosed is not None:
            _untrusted(log, unclosed, "is no JSON event and no torn_tail_closed marker follows it")
        if persist and not _PROPOSAL_ID.fullmatch(str(event.get("proposal_id"))):
            _untrusted(log, number, "is a learning_persist line naming no proposal")
        unclosed = unclosed if isinstance(event, dict) else number
        claimed = claimed or persist and event["proposal_id"] == proposal_id
        before, start = (start, raw[:-1]), start + len(raw)  # a line with a successor always ends in \n
    if unclosed is not None and raw.endswith(b"\n"):
        _untrusted(log, unclosed, "is no JSON event and no torn_tail_closed marker follows it")
    return claimed


def _claimed_in_log(state_dir: Path, proposal_id: str) -> bool:
    """Whether the ledger holds a ``learning_persist`` line for ``proposal_id`` (a missing log holds none).

    Read under the log's shared flock, so an append made under the lock is seen whole or not at all. A torn last line is
    skipped. A line that is no JSON event is accepted only as a torn tail an append closed: the next line must be
    the ``torn_tail_closed`` marker whose offset, length and sha256 match its bytes, and it never counts as a claim.
    A torn ``learning_persist`` fragment is therefore no claim, rightly: the ledger line comes before ``consumed_at``,
    so a crash that tore it left the record unmarked too and the approval stays usable. Any other line that is no
    JSON event, a marker that does not match the line before it, or a persist line naming no proposal refuses."""
    log = Path(state_dir) / USAGE_LOG_NAME
    try:
        with open(log, "rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            return _claimed_in_lines(log, handle, proposal_id)
    except FileNotFoundError:
        return False
    except OSError as exc:
        _refuse(f"{log} is unreadable ({exc}), so whether {proposal_id} was used is unknown")


def _torn_tail(fd: int, size: int) -> Tuple[int, bytes]:
    """``(offset, bytes)`` of a last line without ``\\n`` (a torn append); ``(size, b"")`` when none is torn."""
    start = size
    while start > 0:
        step = min(start, 1 << 16)
        cut = os.pread(fd, step, start - step).rfind(b"\n")
        start -= step
        if cut >= 0:
            start += cut + 1
            break
    return start, os.pread(fd, size - start, start)


def append_usage_event(state_dir: Path, event: Dict[str, Any]) -> None:
    """The only way to append to ``intelligence_usage.ndjson`` (OI-2714). One line under the log's flock. A torn last line is first closed with ``\\n`` and followed by a
    ``torn_tail_closed`` marker naming its offset, length and sha256, so ``_claimed_in_log`` can tell it from a
    corrupted line. Append-only (ADR-005): no byte is removed. One fsync covers all three."""
    with open(Path(state_dir) / USAGE_LOG_NAME, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        offset, fragment = _torn_tail(handle.fileno(), handle.seek(0, os.SEEK_END))
        closed = b"\n" + _canonical(_torn_tail_marker(offset, fragment)).encode("utf-8") + b"\n" if fragment else b""
        handle.write(closed + _canonical(event).encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _append_event(state_dir: Path, event: Dict[str, Any]) -> None:
    """The seam ``claim_approval`` and ``write_record`` append through; ``append_usage_event`` does the work."""
    append_usage_event(state_dir, event)


def _guard_writes(state_dir: Path, path: Path) -> None:
    for target in (path, Path(state_dir) / USAGE_LOG_NAME):
        refuse_real_central_store_write_under_test_runner(target)


def claim_approval(state_dir: Path, valid_approvers: Collection[str], changes: List[Dict[str, Any]]) -> str:
    """Bind the selected record to ``changes`` and use it up before the first DB write; returns the
    record sha256. Re-read under the lock: of two concurrent persists on one record only one gets through. The
    ledger line is the decision and comes first; ``consumed_at`` in the record follows it."""
    record = load_approval(state_dir, valid_approvers)
    if not isinstance(changes, list) or not all(isinstance(row, dict) for row in changes):
        _refuse("the rows to write are no canonical JSON (a list of JSON objects)")
    try:
        actual = preview(changes)["changes_sha256"]
    except (TypeError, ValueError) as exc:
        _refuse(f"the rows to write are no canonical JSON ({exc})")
    if actual != record["changes_sha256"]:
        _refuse(f"the rows to write hash to {actual}, the approval is for {record['changes_sha256']}")
    path = record_path(state_dir, record["proposal_id"])
    _guard_writes(state_dir, path)
    with slot_lock(path):
        if load_approval(state_dir, valid_approvers) != record:
            _refuse(f"approval record {path} changed while this persist read it")
        record_sha256, consumed_at = _sha256(record), datetime.now(timezone.utc).isoformat()
        try:
            _append_event(state_dir, {"event_type": "learning_persist", "timestamp": consumed_at, "rows": len(changes),
                                      "proposal_id": record["proposal_id"], "changes_sha256": actual,
                                      "approval_record_sha256": record_sha256, "approved_by": record["approved_by"]})
        except OSError as exc:
            raise LearningApprovalRefused(f"appending the learning_persist line failed ({exc}): the record is "
                                          "untouched, no row was written; the approval is used only if that line "
                                          "reached the log") from exc
        try:
            atomic_write_json(path, dict(record, consumed_at=consumed_at))
        except OSError as exc:
            raise LearningApprovalRefused(f"the ledger holds the claim but record {path} was not marked used ({exc}); "
                                          "no row was written and the approval counts as used") from exc
    return record_sha256


def write_record(state_dir: Path, proposal_id: str, approved_by: str, reason: str,
                 valid_approvers: Collection[str]) -> Path:
    """Create an unused record under its lock, its ``learning_approval`` event first. Never overwrites a record and
    refuses a proposal the ledger already shows used, since such a record could never be claimed."""
    digest = _digest_of(proposal_id)
    if not isinstance(approved_by, str) or approved_by not in valid_approvers:
        _refuse(f"approved_by {approved_by!r} is not one of {sorted(valid_approvers)}")
    if not isinstance(reason, str) or not reason.strip():
        _refuse("an approval needs a non-empty --reason")
    path = record_path(state_dir, proposal_id)
    _guard_writes(state_dir, path)
    record = {"proposal_id": proposal_id, "changes_sha256": digest, "approved_by": approved_by,
              "approved_at": datetime.now(timezone.utc).isoformat(), "reason": reason.strip(), "consumed_at": None}
    with slot_lock(path):
        if path.exists() or path.is_symlink():
            _refuse(f"{path} already exists; an approval record is never overwritten")
        if _claimed_in_log(state_dir, proposal_id):
            _refuse(f"approval {proposal_id} was already used: {USAGE_LOG_NAME} holds its learning_persist line")
        try:
            _append_event(state_dir, {"event_type": "learning_approval", "timestamp": record["approved_at"],
                                      "proposal_id": proposal_id, "changes_sha256": digest,
                                      "approval_record_sha256": _sha256(record), "approved_by": approved_by})
        except OSError as exc:
            raise LearningApprovalRefused(f"appending the learning_approval line failed ({exc}); "
                                          "no record was written") from exc
        try:
            atomic_write_json(path, record)
        except OSError as exc:
            raise LearningApprovalRefused(f"the ledger holds the learning_approval line but record {path} was not "
                                          f"written ({exc}); no approval exists, approve again") from exc
    return path


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approve one learning-loop proposal (ADR-068, besluit 18).")
    commands = parser.add_subparsers(dest="command", required=True)
    approve = commands.add_parser("approve", help="create the approval record; needs an interactive terminal")
    approve.add_argument("--proposal-id", required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--approved-by", default="operator")
    args = parser.parse_args(argv)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("REFUSED: approve needs an interactive terminal (stdin and stdout a tty); no record written",
              file=sys.stderr)
        return 1
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from learning_loop import _DISPOSITION_VALID_APPROVERS
    from vnx_paths import ensure_env

    state_dir = Path(ensure_env()["VNX_STATE_DIR"]).expanduser().resolve()
    try:
        path = write_record(state_dir, args.proposal_id, args.approved_by, args.reason, _DISPOSITION_VALID_APPROVERS)
    except LearningApprovalRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"approved: {path}\nnext: {APPROVED_ENV}={args.proposal_id} VNX_LEARNING_LOOP_PERSIST=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
