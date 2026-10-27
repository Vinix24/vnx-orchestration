"""One gate outcome, one writer: :class:`GateOutcome` and :func:`commit_gate_outcome` (ADR-046).

Partial-failure contract (ADR-046 §3), fixed here before the code:

1. **Order.** The record is written first, atomically (temp file plus
   ``os.replace``, ``gate_recorder._write_result_atomic``), then the one
   ``review_gate_result`` receipt.
2. **Partial failure.** If the receipt append fails, the record stays with
   ``receipt_pending: true``. The flag is on disk before the append is tried,
   so a process that dies between the two leaves the same mark; a booked
   receipt clears it with a second atomic write. A writer's own receipt
   fields (:attr:`GateOutcome.receipt_fields`, GE-4c-1) sit next to the flag
   as ``receipt_fields`` and go with it, so a receipt booked later carries them.
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
   (same ``commit_sha``, status, pass/fail decision and evidence,
   :data:`REPEAT_EVIDENCE_FIELDS`) is a no-op: no rewrite, no
   ``attempt_seq`` increment, no receipt; it returns the stored record. The
   key carries no ``attempt_seq``, so a rewrite advanced the attempt while the
   key returned its receipt as a duplicate. Only decided records: an
   ``unavailable`` or ``not_executable`` owes no receipt, and its newer
   ``reason_detail`` (a quota refusal on a retry) still lands and feeds the
   provider-reachability ledger. A write that changes the status or the
   evidence is no repeat and lands as the next attempt with its own receipt,
   unless that receipt has the identity of the one booked for the record on
   the slot (``gate_recorder.outcome_receipt_identity``: the idempotency key,
   or the outcome id of a receipt that names a dispatch). The append would
   return it as a duplicate and the record would change while the ledger
   keeps the old line, so it is refused (:class:`DuplicateReceiptRefused`)
   before the round ledger, like every refusal of step 5.
7. **Request** (GE-4b). An admitted outcome, a repeat included, then gives
   its :class:`GateRequest` the terminal status of the record on the slot
   (:data:`REQUEST_STATUS_OF`, or the request's own :attr:`GateRequest.status`),
   under the request path's lock inside this one. A refusal changes no
   request; a failed request write is logged.

All of it runs under ``atomic_io.slot_lock`` on the record path. The forge
publication, bundle archive and reachability feed stay outside, as before.

Limits: the key deduplicates within the append cache window (300 s), so a
receipt that landed, whose clearing write then failed, and that is recovered
after that window, can book a second line. The writers still on
``write_result_guarded`` (``gate_request_handler``, a ci_gate ``running``
snapshot) stamp no ``attempt_seq``, and no obligation is booked here (GE-4c-2).
A ``claude_github_optional`` record carries no head of its own (its receipt
reads the head off the request), so it is never a repeat: an identical retry
lands as the next attempt, and the key returns its receipt as a duplicate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import gate_recorder
from atomic_io import slot_lock
from gate_status import FAIL_STATES, PARTIAL_REVIEW_STATES, PASS_STATES, UNAVAILABLE_STATES, canonical_status, is_pass

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
# A request's terminal status per status of the record that answers it (step 7).
# A pass, a fail and a partial review all completed the requested run, as
# gate_artifacts and ci_gate booked it before. A lookup miss ends no request.
REQUEST_STATUS_OF: Dict[str, str] = {
    **dict.fromkeys(PASS_STATES | FAIL_STATES | PARTIAL_REVIEW_STATES, "completed"),
    **dict.fromkeys(UNAVAILABLE_STATES, "unavailable"), "not_executable": "not_executable",
}

# The receipt append's wait for its lock while the slot lock is held. A lock
# still taken then raises before anything is written: step 2 of the contract.
RECEIPT_LOCK_TIMEOUT_SECONDS = 30.0

# Step 6: the evidence a repeat carries unchanged, next to head, status and
# decision. Nothing else joins: not report_path, recorded_at or attempt_seq.
REPEAT_EVIDENCE_FIELDS: Tuple[str, ...] = ("blocking_findings", "advisory_findings", "required_reruns", "residual_risk")


class ReceiptPendingRefused(gate_recorder.ResultOverwriteRefused):
    """The record on the slot still owes its receipt and booking it failed again (step 3).

    A :class:`gate_recorder.ResultOverwriteRefused`, so every writer's existing
    refusal path handles it: nothing is written, nothing is published.
    """


class DuplicateReceiptRefused(gate_recorder.ResultOverwriteRefused):
    """A write changes the outcome on the slot, and its receipt would be the duplicate of that record's (step 6).

    Handled by every writer's existing refusal path, like :class:`ReceiptPendingRefused`.
    """


# Refusals whose own text replaces the guard's in the audit line.
_REFUSALS_WITH_OWN_CAUSE = (ReceiptPendingRefused, DuplicateReceiptRefused)


@dataclass(frozen=True)
class GateOutcome:
    """One outcome for one slot ``(pr, gate)``.

    ``record`` is the result record, whole: its keys differ per lane, and the
    writer renames or drops none. It stamps ``attempt_seq`` and
    ``receipt_pending`` on it in place, so the caller's dict is the record on disk.
    ``receipt_fields`` are the writer's own fields on the receipt, next to the
    generic ones (gate_result_parser's findings, summary and residual risk);
    they add to those and replace none.
    """

    gate: str
    status: str
    pr_id: str
    pr_number: Optional[int]
    record: Dict[str, Any]
    receipt_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.gate or self.status not in OUTCOME_STATUSES:
            raise ValueError(f"gate={self.gate!r} status={self.status!r}: an outcome is one of {OUTCOME_STATUSES}")

    @classmethod
    def from_record(
        cls, gate: str, record: Dict[str, Any], *, pr_id: str, downgraded: bool = False,
        receipt_fields: Optional[Mapping[str, Any]] = None,
    ) -> "GateOutcome":
        """``downgraded``: the writer downgraded a pass on a cut diff itself (gate_artifacts)."""
        record_status = canonical_status(record)
        status = OUTCOME_OF.get(record_status, "")
        if downgraded and record_status in PARTIAL_REVIEW_STATES:
            status = "pass"
        if not status:
            raise ValueError(
                f"{gate} result for pr={pr_id!r} has status {record.get('status')!r}: "
                f"an outcome is one of {OUTCOME_STATUSES} (ADR-046)"
            )
        pr_number = record.get("pr_number")
        if not isinstance(pr_number, int) or isinstance(pr_number, bool):
            pr_number = None
        return cls(gate=gate, status=status, pr_id=pr_id, pr_number=pr_number, record=record,
                   receipt_fields=dict(receipt_fields or {}))

    @property
    def pr_ref(self) -> str:
        return self.pr_id or str(self.pr_number or "")


@dataclass(frozen=True)
class GateRequest:
    """The request an outcome answers (step 7); ``fields`` land on it only when the outcome is admitted.

    ``status`` is the request's end when its writer names one itself
    (``record_failure``: a failed run ends ``failed``, as it did before GE-4c);
    otherwise :data:`REQUEST_STATUS_OF` decides.
    """

    requests_dir: Path
    payload: Dict[str, Any]
    fields: Mapping[str, Any] = field(default_factory=dict)
    status: Optional[str] = None


def commit_gate_outcome(
    outcome: GateOutcome,
    *,
    results_dir: Path,
    result_path: Optional[Path] = None,
    audit_refusal: bool = True,
    raise_on_refusal: bool = False,
    request: Optional[GateRequest] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Write one outcome's record, its one receipt and its request's end under the slot's lock (ADR-046 §2).

    ``result_path`` names the slot when its file name is not
    ``gate_recorder.result_file_path``'s. Returns ``(payload_on_disk,
    written)`` like ``write_result_guarded``; ``raise_on_refusal`` raises the
    guard's ``ResultOverwriteRefused`` instead, after the audit line and the
    bundle archive. ``audit_refusal`` is ``write_result_guarded``'s opt-out.
    A repeat (step 6) returns ``(stored_record, True)``: the slot holds this
    outcome and nothing was refused, but nothing was written or published
    either, and the caller's ``record`` is left unstamped. ``request`` is the
    request this outcome answers (step 7).
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
            _refuse_a_duplicate_receipt(path, existing, outcome)
            gate_recorder._book_round(path, record, gate=gate)
        except gate_recorder.ResultOverwriteRefused as exc:
            refusal = exc
            if audit_refusal:
                detail = str(exc) if isinstance(exc, _REFUSALS_WITH_OWN_CAUSE) else None
                gate_recorder._audit_write_refusal(path, gate, pr_ref, record, reason_detail=detail)
            on_disk = existing if isinstance(existing, dict) else {}
        else:
            if _repeats_the_slot(existing, record):
                repeated = existing
            else:
                _write_record_then_receipt(
                    path, record, existing, gate=gate, pr_ref=pr_ref, receipt_fields=outcome.receipt_fields,
                )
            if request is not None:
                _finish_request(request, repeated or record, outcome)
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
    non-empty head, status and pass/fail decision are equal (the receipt
    books ``is_pass``, so a ``completed`` with blocking findings is a fail),
    and so is the evidence (:func:`_changed_evidence`). Anything missing or
    malformed is no repeat, and the write goes ahead.
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
            and is_pass(existing)[0] == is_pass(record)[0]
            and not _changed_evidence(existing, record))


def _changed_evidence(existing: Dict[str, Any], record: Dict[str, Any]) -> List[str]:
    """The :data:`REPEAT_EVIDENCE_FIELDS` that differ: absent on both sides is equal, on one side a change."""
    return [name for name in REPEAT_EVIDENCE_FIELDS if existing.get(name) != record.get(name)]


def _refuse_a_duplicate_receipt(path: Path, existing: Any, outcome: GateOutcome) -> None:
    """Step 6: refuse a decided write that changes the outcome on the slot when the key would drop its receipt.

    Only a write that is no repeat and changes the status or the evidence of
    a decided record. That record's receipt is taken as booked (step 3 paid
    any debt first) under the identity this writer gives it now. Its own
    ``receipt_fields`` left the record with that booking, so a key they filled
    (claude_github's head, which lives on its request and not on its record)
    is read as this write's: the booked receipt cannot be read back, and
    reading it as different would let the key swallow this write.
    """
    record = outcome.record
    if not isinstance(existing, dict) or _repeats_the_slot(existing, record):
        return
    if not (gate_recorder.owes_outcome_receipt(existing) and gate_recorder.owes_outcome_receipt(record)):
        return
    changed = ["status"] if canonical_status(existing) != canonical_status(record) else []
    changed += _changed_evidence(existing, record)
    if not changed:
        return
    old_key, old_outcome_id = gate_recorder.outcome_receipt_identity(
        existing, gate=outcome.gate, pr_ref=outcome.pr_ref, extra_fields=outcome.receipt_fields,
    )
    new_key, new_outcome_id = gate_recorder.outcome_receipt_identity(
        record, gate=outcome.gate, pr_ref=outcome.pr_ref, extra_fields=outcome.receipt_fields,
    )
    if new_key == old_key:
        same = "idempotency key"
    elif new_outcome_id and new_outcome_id == old_outcome_id:
        same = "outcome id (dispatch_id, head, status, gate and PR)"
    else:
        return
    raise DuplicateReceiptRefused(
        f"{outcome.gate} result for pr={outcome.pr_ref!r} at {path}: this write changes "
        f"{', '.join(changed)} of the decided outcome on the slot (commit_sha={existing.get('commit_sha')!r}, "
        f"status={canonical_status(existing)!r}, attempt_seq={existing.get('attempt_seq')!r}), but its "
        f"review_gate_result receipt has the {same} of the receipt booked for that record: the append "
        f"would return it as a duplicate and the record would change while the ledger keeps the old "
        f"line; refusing it (ADR-046 §6)"
    )


def _write_record_then_receipt(
    path: Path, record: Dict[str, Any], existing: Any, *, gate: str, pr_ref: str,
    receipt_fields: Mapping[str, Any],
) -> None:
    """Steps 1, 2 and 4; the caller holds the slot's lock and the guard admitted the write."""
    seq = existing.get("attempt_seq") if isinstance(existing, dict) else None
    record["attempt_seq"] = seq + 1 if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1 else 1
    owes_receipt = gate_recorder.owes_outcome_receipt(record)
    if owes_receipt:
        record["receipt_pending"] = True
        if receipt_fields:
            record["receipt_fields"] = dict(receipt_fields)
    else:
        record.pop("receipt_pending", None)
    gate_recorder._write_result_atomic(path, record)
    if owes_receipt:
        _book_receipt(path, record, gate=gate, pr_ref=pr_ref)


def _finish_request(request: GateRequest, record: Dict[str, Any], outcome: GateOutcome) -> None:
    """Step 7; the caller holds the slot's lock and the record on the slot answers the request."""
    status = REQUEST_STATUS_OF.get(canonical_status(record))
    if status is None:
        logger.warning("gate_outcome: %s request pr=%s left as it was: status %r ends no request",
                       outcome.gate, outcome.pr_ref, record.get("status"))
        return
    status = request.status or status
    request.payload.update(request.fields)
    request.payload["status"] = status
    try:
        gate_recorder.persist_request(
            request.requests_dir, outcome.gate, request.payload, pr_number=outcome.pr_number, pr_id=outcome.pr_id,
        )
    except OSError as exc:
        logger.warning("gate_outcome: %s request pr=%s NOT set to %s (the record landed) — %s: %s",
                       outcome.gate, outcome.pr_ref, status, type(exc).__name__, exc)


def _book_receipt(path: Path, record: Dict[str, Any], *, gate: str, pr_ref: str) -> bool:
    """Book the record's receipt, then clear its flag; a failed append leaves the flag.

    Only ``receipt_pending is True`` is a debt (the caller checks): a missing,
    ``None`` or other value was not set by this writer and is left alone.
    Returns ``False`` only when the append raised. ``True`` means nothing is
    owed any more: the append returned (appended or duplicate), or the record
    owes no receipt; a failed clearing write afterwards does not change that.
    The writer's own ``receipt_fields`` travel with the flag.
    """
    extra = record.get("receipt_fields")
    try:
        gate_recorder._emit_terminal_result_receipt(
            record, gate=gate, pr_ref=pr_ref,
            lock_timeout=RECEIPT_LOCK_TIMEOUT_SECONDS, raise_on_failure=True,
            # Only this writer sets it, always as a dict. Anything else on a
            # record from disk books the generic receipt: the debt is paid
            # with what can be read rather than block the slot for ever.
            extra_fields=extra if isinstance(extra, dict) else None,
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
    record.pop("receipt_fields", None)
    try:
        gate_recorder._write_result_atomic(path, record)
    except OSError as exc:
        # Booked, but the flag on disk still claims the debt: the next commit
        # books it again and the key returns a duplicate. Restored so the
        # caller's dict matches the record on disk.
        record["receipt_pending"] = True
        if extra is not None:
            record["receipt_fields"] = extra
        logger.warning(
            "gate_outcome: receipt booked for gate=%s pr=%s, receipt_pending not cleared — %s: %s",
            gate, pr_ref, type(exc).__name__, exc,
        )
    return True
