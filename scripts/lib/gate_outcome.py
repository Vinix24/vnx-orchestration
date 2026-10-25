"""One gate outcome, one writer: :class:`GateOutcome` and :func:`commit_gate_outcome` (ADR-046).

Partial-failure contract (ADR-046 §3), fixed here before the code:

1. **Order.** The record is written first, atomically (temp file plus
   ``os.replace``, ``gate_recorder._write_result_atomic``), then the one
   ``review_gate_result`` receipt.
2. **Partial failure.** If the receipt append fails, the record stays with
   ``receipt_pending: true``. The flag is on disk before the append is tried,
   so a process that dies between the two leaves the same mark; a booked
   receipt clears it with a second atomic write.
3. **Recovery.** The next ``commit_gate_outcome`` on the same slot books the
   missing receipt first, idempotently through the existing key with
   ``commit_sha`` and ``status`` (``append_receipt_internals/idempotency.py``),
   so a receipt that did land comes back as a duplicate, not a second line.
   If that booking fails again, the commit is refused
   (:class:`ReceiptPendingRefused`) before the guard and the round ledger: the
   record keeps its flag byte for byte and nothing new is written or emitted.
   Replacing it would drop the only trace of the unbooked receipt; there is no
   second queue for it.
4. **Attempt identity** is ``(pr, gate, commit_sha, attempt_seq)``;
   ``attempt_seq`` increases within the slot (1 on an empty slot).
5. **Refusal.** A write the overwrite guard or the round ledger refuses writes
   no record and no receipt, and sets no flag. The debt of the record that
   stays is still paid by step 3: it belongs to that record.
6. **Repeat.** An admitted write of the decided outcome already on the slot
   (same ``commit_sha``, status and pass/fail decision) is a no-op: no
   rewrite, no ``attempt_seq`` increment, no receipt; it returns the stored
   record. The key carries no ``attempt_seq``, so a rewrite advanced the
   attempt while the key returned its receipt as a duplicate. Only decided
   records: an ``unavailable`` or ``not_executable`` owes no receipt, and its
   newer ``reason_detail`` (a quota refusal on a retry) still lands and feeds
   the provider-reachability ledger.

All of it runs under ``atomic_io.slot_lock`` on the record path. The forge
publication, bundle archive and reachability feed stay outside, as before.

Limits: the key deduplicates within the append cache window (300 s), so a
receipt that landed, whose clearing write then failed, and that is recovered
after that window, can book a second line. And only ``record_terminal_result``
and ``record_not_executable`` route through here; the writers still on
``write_result_guarded`` stamp no ``attempt_seq`` (GE-4b moves them).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import gate_recorder
from atomic_io import slot_lock
from gate_status import FAIL_STATES, PASS_STATES, UNAVAILABLE_STATES, canonical_status, is_pass

logger = logging.getLogger(__name__)

# ADR-046 §1. ``partial_review`` is not in it: that is the writer's downgrade of
# a ``pass`` on a cut diff, made on the record, never a caller's choice.
OUTCOME_STATUSES: Tuple[str, ...] = ("pass", "fail", "unavailable", "not_executable")
# Every canonical record status that IS an outcome; in-flight, ``partial_review``,
# empty and unknown statuses are absent, so a lookup miss is a refusal.
OUTCOME_OF: Dict[str, str] = {
    **dict.fromkeys(PASS_STATES, "pass"), **dict.fromkeys(FAIL_STATES, "fail"),
    **dict.fromkeys(UNAVAILABLE_STATES, "unavailable"), "not_executable": "not_executable",
}

# The receipt append's wait for its lock while the slot lock is held. A lock
# still taken then raises before anything is written: step 2 of the contract.
RECEIPT_LOCK_TIMEOUT_SECONDS = 30.0


class ReceiptPendingRefused(gate_recorder.ResultOverwriteRefused):
    """The record on the slot still owes its receipt and booking it failed again (step 3).

    A :class:`gate_recorder.ResultOverwriteRefused`, so every writer's existing
    refusal path handles it: nothing is written, nothing is published.
    """


@dataclass(frozen=True)
class GateOutcome:
    """One outcome for one slot ``(pr, gate)``.

    ``record`` is the result record, whole: its keys differ per lane, and the
    writer renames or drops none. It stamps ``attempt_seq`` and
    ``receipt_pending`` on it in place, so the caller's dict is the record on disk.
    """

    gate: str
    status: str
    pr_id: str
    pr_number: Optional[int]
    record: Dict[str, Any]

    def __post_init__(self) -> None:
        if not self.gate or self.status not in OUTCOME_STATUSES:
            raise ValueError(f"gate={self.gate!r} status={self.status!r}: an outcome is one of {OUTCOME_STATUSES}")

    @classmethod
    def from_record(cls, gate: str, record: Dict[str, Any], *, pr_id: str) -> "GateOutcome":
        status = OUTCOME_OF.get(canonical_status(record), "")
        if not status:
            raise ValueError(
                f"{gate} result for pr={pr_id!r} has status {record.get('status')!r}: "
                f"an outcome is one of {OUTCOME_STATUSES} (ADR-046)"
            )
        pr_number = record.get("pr_number")
        if not isinstance(pr_number, int) or isinstance(pr_number, bool):
            pr_number = None
        return cls(gate=gate, status=status, pr_id=pr_id, pr_number=pr_number, record=record)

    @property
    def pr_ref(self) -> str:
        return self.pr_id or str(self.pr_number or "")


def commit_gate_outcome(
    outcome: GateOutcome,
    *,
    results_dir: Path,
    result_path: Optional[Path] = None,
    audit_refusal: bool = True,
    raise_on_refusal: bool = False,
) -> Tuple[Dict[str, Any], bool]:
    """Write one outcome's record and its one receipt under the slot's lock (ADR-046 §2).

    ``result_path`` names the slot when its file name is not
    ``gate_recorder.result_file_path``'s. Returns ``(payload_on_disk,
    written)`` like ``write_result_guarded``; ``raise_on_refusal`` raises the
    guard's ``ResultOverwriteRefused`` instead, after the audit line and the
    bundle archive. ``audit_refusal`` is ``write_result_guarded``'s opt-out.
    A repeat (step 6) returns ``(stored_record, True)``: the slot holds this
    outcome and nothing was refused, but nothing was written or published
    either, and the caller's ``record`` is left unstamped.
    """
    path = result_path or gate_recorder.result_file_path(
        results_dir, outcome.gate, outcome.pr_number, outcome.pr_id,
    )
    if path is None:
        raise ValueError(f"{outcome.gate} outcome names no PR: there is no slot to write it to")
    record, gate, pr_ref = outcome.record, outcome.gate, outcome.pr_ref
    refusal: Optional[gate_recorder.ResultOverwriteRefused] = None
    on_disk: Dict[str, Any] = {}
    repeated: Optional[Dict[str, Any]] = None
    with slot_lock(path):
        existing = gate_recorder._read_existing_result(path)
        try:
            _pay_outstanding_receipt(path, existing, gate=gate, pr_ref=pr_ref)
            gate_recorder._check_overwrite_guard(path, record, gate=gate, pr_ref=pr_ref)
            gate_recorder._book_round(path, record, gate=gate)
        except gate_recorder.ResultOverwriteRefused as exc:
            refusal = exc
            if audit_refusal:
                detail = str(exc) if isinstance(exc, ReceiptPendingRefused) else None
                gate_recorder._audit_write_refusal(path, gate, pr_ref, record, reason_detail=detail)
            on_disk = existing if isinstance(existing, dict) else {}
        else:
            if _repeats_the_slot(existing, record):
                repeated = existing
            else:
                _write_record_then_receipt(path, record, existing, gate=gate, pr_ref=pr_ref)
    gate_recorder._archive_finished_gate_bundle(record, gate=gate, result_path=path)
    if refusal is not None:
        if raise_on_refusal:
            raise refusal
        return on_disk or record, False
    if repeated is not None:
        return repeated, True
    gate_recorder.publish_forge_check_run(record, gate=gate, result_path=path)
    gate_recorder._feed_provider_reachability(record, gate=gate, result_path=path)
    return record, True


def _pay_outstanding_receipt(path: Path, existing: Any, *, gate: str, pr_ref: str) -> None:
    """Step 3: book the receipt the record on the slot still owes, or refuse to replace it."""
    if not (isinstance(existing, dict) and existing.get("receipt_pending") is True):
        return
    if _book_receipt(path, existing, gate=gate, pr_ref=pr_ref):
        return
    raise ReceiptPendingRefused(
        f"{gate} result for pr={pr_ref!r} at {path}: the record on the slot "
        f"(commit_sha={existing.get('commit_sha')!r}, status={existing.get('status')!r}, "
        f"attempt_seq={existing.get('attempt_seq')!r}) still owes its review_gate_result "
        f"receipt and booking it failed again; refusing to replace it until that receipt "
        f"is booked (ADR-046 §3)"
    )


def _repeats_the_slot(existing: Any, record: Dict[str, Any]) -> bool:
    """Step 6: does this write repeat the decided outcome on the slot?

    Positive evidence only: both records owe a receipt, both name the same
    non-empty head, and status and pass/fail decision are equal (the receipt
    books ``is_pass``, so a ``completed`` with blocking findings is a fail).
    Anything missing or malformed is no repeat, and the write goes ahead.
    """
    if not isinstance(existing, dict):
        return False
    if not (gate_recorder.owes_outcome_receipt(existing) and gate_recorder.owes_outcome_receipt(record)):
        return False
    old_head, new_head = existing.get("commit_sha"), record.get("commit_sha")
    if not (isinstance(old_head, str) and isinstance(new_head, str) and old_head.strip()):
        return False
    return (old_head.strip() == new_head.strip()
            and canonical_status(existing) == canonical_status(record)
            and is_pass(existing)[0] == is_pass(record)[0])


def _write_record_then_receipt(
    path: Path, record: Dict[str, Any], existing: Any, *, gate: str, pr_ref: str,
) -> None:
    """Steps 1, 2 and 4; the caller holds the slot's lock and the guard admitted the write."""
    seq = existing.get("attempt_seq") if isinstance(existing, dict) else None
    record["attempt_seq"] = seq + 1 if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1 else 1
    owes_receipt = gate_recorder.owes_outcome_receipt(record)
    if owes_receipt:
        record["receipt_pending"] = True
    else:
        record.pop("receipt_pending", None)
    gate_recorder._write_result_atomic(path, record)
    if owes_receipt:
        _book_receipt(path, record, gate=gate, pr_ref=pr_ref)


def _book_receipt(path: Path, record: Dict[str, Any], *, gate: str, pr_ref: str) -> bool:
    """Book the record's receipt, then clear its flag; a failed append leaves the flag.

    Only ``receipt_pending is True`` is a debt (the caller checks): a missing,
    ``None`` or other value was not set by this writer and is left alone.
    Returns ``False`` only when the append raised. ``True`` means nothing is
    owed any more: the append returned (appended or duplicate), or the record
    owes no receipt; a failed clearing write afterwards does not change that.
    """
    try:
        gate_recorder._emit_terminal_result_receipt(
            record, gate=gate, pr_ref=pr_ref,
            lock_timeout=RECEIPT_LOCK_TIMEOUT_SECONDS, raise_on_failure=True,
        )
    # vnx-broad-except: the record is on disk with receipt_pending, which is
    # this failure's durable trace and the next commit's work order. The
    # append's failure surface is open-ended; none of it may fail a verdict
    # that already landed.
    except Exception as exc:
        logger.warning(
            "gate_outcome: review_gate_result receipt NOT booked for gate=%s pr=%s head=%s "
            "attempt=%s — %s: %s. The record keeps receipt_pending: true; the next commit "
            "on this slot books it, and no commit replaces it before then.",
            gate, pr_ref, record.get("commit_sha"), record.get("attempt_seq"), type(exc).__name__, exc,
        )
        return False
    record.pop("receipt_pending", None)
    try:
        gate_recorder._write_result_atomic(path, record)
    except OSError as exc:
        # Booked, but the flag on disk still claims the debt: the next commit
        # books it again and the key returns a duplicate. Restored so the
        # caller's dict matches the record on disk.
        record["receipt_pending"] = True
        logger.warning(
            "gate_outcome: receipt booked for gate=%s pr=%s, receipt_pending not cleared — %s: %s",
            gate, pr_ref, type(exc).__name__, exc,
        )
    return True
