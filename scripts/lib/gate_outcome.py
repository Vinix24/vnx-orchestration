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
   before the round ledger, like every refusal of step 5. The one undecided
   exception is the writer that books its undecided records' receipts
   (``book_undecided_receipt``, GE-8): a write whose record is the answer
   already on the slot — same status, same keys, bar the ones that record when
   and how often it was written — is a repeat too, so a commit that only has
   to finish that record's request rewrites nothing.
7. **Request** (GE-4b). An admitted outcome, a repeat included, then gives
   its :class:`GateRequest` the terminal status of the record on the slot
   (:data:`REQUEST_STATUS_OF`, or the request's own :attr:`GateRequest.status`),
   under the request path's lock inside this one. A failed request write is
   never only logged (GE-4c, kimi advisory (b)): the record stays and the
   commit raises :class:`RequestWriteFailed` after the bundle archive.
8. **Obligation** (GE-4c). An admitted outcome, a repeat included, books the
   :class:`GateObligation` it was given here, through
   ``gate_obligations.book_obligation_outcome``. The record is written with
   ``obligation_pending``/``obligation_booking`` BEFORE the booking is tried —
   on the repeat path too, where the stamp goes onto the record already on the
   slot (:func:`_persist_obligation_work_order`) — so a process that dies in
   between, or a booking that raises, leaves a work order the next commit on
   the slot books exactly once; one that cannot pay it refuses
   (:class:`ObligationPendingRefused`) rather than replace that trace. No
   commit returns success with neither a booking nor a work order on disk.
9. **Refusal** (GE-4c, kimi advisory (a)). A request handed to a REFUSED
   commit is ended — the writer's own terminal end when it named one, else
   ``failed`` with ``failure_reason: write_refused`` — so ``execute_gate``
   short-circuits on it (:data:`gate_status.REQUEST_TERMINAL_STATUSES`) and a
   real retry is a NEW request record instead of a re-run into the same refusal.
   Only the request the caller actually HOLDS may be ended (GE-4c round 2): the
   request record on the slot is read under its own lock and its identity
   (:data:`REQUEST_IDENTITY_FIELDS`) and status are compared first, so a
   refusal never overwrites a request another writer already completed, nor a
   newer retry in the same ``(gate, PR)`` slot. Both cases leave the record —
   completion metadata included — exactly as it is and return a named
   :class:`RequestLeftAlone` (:func:`_foreign_request_reason`).

All of it runs under ``atomic_io.slot_lock`` on the record path. The forge
publication, bundle archive and reachability feed stay outside, as before.

Limits: the key deduplicates within the append cache window (300 s), so a
receipt that landed, whose clearing write then failed, and that is recovered
after that window, can book a second line. The writers that are no outcome
still stamp no ``attempt_seq``: a ci_gate ``running`` snapshot,
``gate_request_handler``'s optional-gate mirror (both ``write_result_guarded``)
and a takeover annotation on a decided record
(``annotate_result_guarded``, GE-4c-2). Scheduling state of an obligation
(claims, retry counters, ``dispatch_still_running``) is not an outcome and
keeps its own writer, ``gate_obligations.update_obligation``.
A ``claude_github_optional`` record carries no head of its own (its receipt
reads the head off the request), so it is never a repeat: an identical retry
lands as the next attempt, and the key returns its receipt as a duplicate.

**The cost line (K3-2b).** After the slot lock, on EVERY end — a new record, a
repeat and a refused overwrite alike — :func:`commit_gate_outcome` books the
one ``cost_ledger`` line of the execution the record names
(:data:`gate_status.EXECUTION_ID_FIELD`, validated with
``gate_status.is_execution_id``) through
:func:`harness_run_cost.record_attempt_run`. The execution id IS the identity
(K3-1a): the same record committed twice is one line whether or not the slot
took it, and a separate run whose record the guard refused gets its own. The
slot's ``attempt_seq`` is never consulted — it counts slot writes, not
executions, which is the round-4 duplicate of K3 (#2329, OI-2444). A booking
never changes the verdict, the receipt, the request end or the obligation.
Three records are not booked: ``--reprocess`` evidence (it calls no model),
a run that provably never started (it was not a run), and an execution K2b
already measured at its lane's own end. A run that did start and still names
no valid execution id books nothing, logs one WARNING and is counted as
``harness_run_cost.UNBOOKED_NO_EXECUTION_ID``: a record from before K3-2a is
never booked under a guessed identity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

import gate_obligations
import gate_recorder
from atomic_io import slot_lock
from gate_status import (
    EVIDENCE_SOURCE_FIELD, EVIDENCE_SOURCE_REPROCESSED, EXECUTION_ID_FIELD,
    FAIL_STATES, PARTIAL_REVIEW_STATES, PASS_STATES, REQUEST_TERMINAL_STATUSES, UNAVAILABLE_STATES,
    canonical_status, is_execution_id, is_pass,
)

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
# Every value is a member of ``gate_status.REQUEST_TERMINAL_STATUSES`` — the
# same set ``gate_executor.execute_gate`` short-circuits on, so a request ended
# here is a request the executor will not re-run (GE-4c, kimi advisory (c)).
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

# Step 6 (GE-8): the keys that say WHEN and HOW OFTEN a record was written, not
# what it answers. Two undecided records that agree on everything else are the
# same answer, re-committed.
_VOLATILE_RECORD_KEYS = frozenset({
    "attempt_seq", "timestamp", "recorded_at", "receipt_pending", "receipt_fields",
    "obligation_pending", "obligation_booking",
})

# Step 8: the two keys the record carries while it owes its obligation booking.
OBLIGATION_PENDING_KEY = "obligation_pending"
OBLIGATION_BOOKING_KEY = "obligation_booking"
# Step 9: what a request handed to a refused commit ends as.
WRITE_REFUSED = "write_refused"

# Step 9 (GE-4c round 2): the fields that identify ONE request in a slot. The
# request's own ``requested_at`` stamp is the request id an attempt holds; the
# rest are what a retry in the same (gate, PR) slot changes first. A field only
# one side names identifies nothing, so it cannot be evidence of a match.
REQUEST_IDENTITY_FIELDS: Tuple[str, ...] = (
    "gate", "pr_number", "pr_id", "commit_sha", "contract_hash", "requested_at",
)
# The named refusals of a write that would end a request the caller does not hold.
REQUEST_NOT_ON_DISK = "request_not_on_disk"
REQUEST_UNREADABLE = "request_unreadable_on_disk"
REQUEST_ALREADY_ENDED = "request_already_terminal"
REQUEST_ANOTHER_ATTEMPT = "request_belongs_to_another_attempt"


class ReceiptPendingRefused(gate_recorder.ResultOverwriteRefused):
    """The record on the slot still owes its receipt and booking it failed again (step 3).

    A :class:`gate_recorder.ResultOverwriteRefused`, so every writer's existing
    refusal path handles it: nothing is written, nothing is published.
    """


class DuplicateReceiptRefused(gate_recorder.ResultOverwriteRefused):
    """A write changes the outcome on the slot, and its receipt would be the duplicate of that record's (step 6).

    Handled by every writer's existing refusal path, like :class:`ReceiptPendingRefused`.
    """


class ObligationPendingRefused(gate_recorder.ResultOverwriteRefused):
    """The record on the slot still owes an obligation booking and booking it failed again (step 8).

    A :class:`gate_recorder.ResultOverwriteRefused`, so every writer's existing
    refusal path handles it; replacing the record would drop the only trace of
    the obligation that was owed.
    """


class RequestWriteFailed(RuntimeError):
    """The record landed, its receipt landed, and ending its request did not (step 7, GE-4c).

    Raised, never only logged (kimi advisory (b)): the request on disk keeps the
    old status while the caller's copy would claim the new one.
    """

    def __init__(self, message: str, *, record: Mapping[str, Any], status: str) -> None:
        super().__init__(message)
        self.record = dict(record)
        self.status = status


@dataclass(frozen=True)
class RequestLeftAlone:
    """A refused write left the request on the slot untouched, because it is not the caller's (step 9).

    ``reason`` is one of the ``REQUEST_*`` names: absent, unreadable, already
    terminal, or another attempt's — and ``detail`` names the fact that
    decided it (the differing identity field, or the terminal status). Returned
    rather than raised: the commit itself was refused for its own reason, and
    leaving a request that belongs to someone else alone is not a second
    failure of this writer.
    """

    reason: str
    detail: str


# Refusals whose own text replaces the guard's in the audit line.
_REFUSALS_WITH_OWN_CAUSE = (ReceiptPendingRefused, DuplicateReceiptRefused, ObligationPendingRefused)


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


@dataclass(frozen=True)
class GateObligation:
    """The obligation booking that belongs to this outcome (step 8, GE-4c).

    ``path`` is the obligation record the dispatch door registered and
    ``fields`` what the outcome says about it (``status``, ``reason``,
    ``reason_detail``, ``result_path``, the provenance a reader joins on). Only
    OUTCOMES belong here: a claim, a retry counter or a ``dispatch_still_running``
    wait is scheduling state and stays with the runner's own writer.
    """

    path: Path
    fields: Mapping[str, Any] = field(default_factory=dict)


def commit_gate_outcome(
    outcome: GateOutcome,
    *,
    results_dir: Path,
    result_path: Optional[Path] = None,
    audit_refusal: bool = True,
    raise_on_refusal: bool = False,
    request: Optional[GateRequest] = None,
    obligation: Optional[GateObligation] = None,
    book_undecided_receipt: bool = False,
) -> Tuple[Dict[str, Any], bool]:
    """Write one outcome's record, its one receipt, its request's end and its obligation under the slot's lock.

    ``result_path`` names the slot when its file name is not
    ``gate_recorder.result_file_path``'s. Returns ``(payload_on_disk,
    written)`` like ``write_result_guarded``; ``raise_on_refusal`` raises the
    guard's ``ResultOverwriteRefused`` instead, after the audit line and the
    bundle archive. ``audit_refusal`` is ``write_result_guarded``'s opt-out.
    A repeat (step 6) returns ``(stored_record, True)``: nothing was refused,
    but nothing was written or published either. ``request`` (step 7) and
    ``obligation`` (step 8) are booked on a repeat too, so a caller that decides
    them after the record landed can hand them to a commit that rewrites nothing.

    K3-2b: the run's one cost line is booked after the slot lock on every end —
    a new record, a repeat and a guard refusal alike — keyed on the record's
    ``execution_id`` (:func:`_book_gate_attempt`), because a run that called a
    model cost money whether or not its record replaced the slot.

    Raises :class:`RequestWriteFailed` when the record and receipt landed but
    the request write did not (step 7), after the bundle archive: the caller
    sees the failure instead of a status the request on disk never reached.

    ``book_undecided_receipt`` (GE-8) is the orphan cleanup's one hook: an
    ``unavailable`` record books its ``review_gate_result`` receipt in the same
    commit, so ending a request that never got an answer leaves a ledger line
    saying so. Off by default; every other writer keeps ADR-046 §1 (only a
    decided pass or fail books an outcome receipt). The record's own status
    travels on the receipt, so the verdict rule reads ``investigate``. It also
    makes an undecided write that repeats the record on the slot a repeat
    (step 6), which is how the cleanup finishes a request whose record landed
    but whose end did not, without writing that record a second time.
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
    request_failure: Optional[RequestWriteFailed] = None
    with slot_lock(path):
        existing = gate_recorder._read_existing_result(path)
        try:
            _pay_outstanding_receipt(path, existing, gate=gate, pr_ref=pr_ref)
            _pay_outstanding_obligation(path, existing)
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
            if _repeats_the_slot(existing, record, undecided_repeats=book_undecided_receipt):
                repeated = existing
                _persist_obligation_work_order(path, repeated, obligation)
            else:
                _write_record_then_receipt(
                    path, record, existing, gate=gate, pr_ref=pr_ref, receipt_fields=outcome.receipt_fields,
                    obligation=obligation, book_undecided_receipt=book_undecided_receipt,
                )
            if obligation is not None:
                _book_obligation(path, repeated or record, obligation)
            if request is not None:
                request_failure = _finish_request(request, repeated or record, outcome)
        if refusal is not None and request is not None:
            # Step 9: the refused run must not be re-run into the same refusal,
            # and a request it does not hold must not be ended at all.
            request_end = _finish_refused_request(
                request, refusal, gate=gate, pr_number=outcome.pr_number, pr_id=outcome.pr_id,
            )
            if isinstance(request_end, RequestWriteFailed):
                request_failure = request_end
    gate_recorder._archive_finished_gate_bundle(record, gate=gate, result_path=path)
    # K3-2b: the run ended here, whatever the slot did with its record. Booked
    # AFTER the slot lock and after the record, its receipt, its request end and
    # its obligation landed, so a slow or broken ledger can neither delay nor
    # undo a verdict; keyed on the record's own execution id, so a repeat and a
    # refused write are the same run's one line and a separate attempt is its own.
    _book_gate_attempt(gate=gate, record=record, result_path=path, pr_ref=pr_ref)
    if request_failure is not None:
        raise request_failure
    if refusal is not None:
        if raise_on_refusal:
            raise refusal
        return on_disk or record, False
    if repeated is not None:
        return repeated, True
    gate_recorder.publish_forge_check_run(record, gate=gate, result_path=path)
    gate_recorder._feed_provider_reachability(record, gate=gate, result_path=path)
    return record, True


# K3-2b: the execution-failure reasons that PROVE no model call happened — the
# door refused the run before it was spawned, or the gate never reached the
# door. Positive evidence only, so this is NOT
# ``gate_recorder.EXECUTION_FAILURE_REASONS``: that set also holds ``timeout``,
# ``stall`` and ``exit_nonzero``, whose run DID start and whose usage is
# unknown, and dropping those would make a real cost invisible — the one
# direction a booking must never fail in.
_NEVER_STARTED_REASONS = frozenset({
    # the door refused before anything was spawned
    "spawn_refused", "lane_exhausted", "provider_switched_off", "provider_disabled",
    "provider_not_installed", "binary_not_found",
    # the gate never reached the door at all
    "gate_runner_missing", "gate_not_subprocess_routable", "unsupported_gate_type",
    # a launch prerequisite failed, so no process (and no model call) ever started
    "worktree_checkout_failed", "gate_dispatch_identity_invalid", "harness_lane_branch_unavailable",
    "harness_lane_no_model_response",
    # not a run at all: ``cleanup_orphan_gates`` ends a stale request it found
    # in a scan (GE-8), so nothing was spawned and nothing was spent
    "abandoned_orphan_request",
})


def _never_started(record: Mapping[str, Any]) -> bool:
    """True only on positive evidence that this record's run reached no model call (K3-2b).

    A ``not_executable`` status, a reason in :data:`_NEVER_STARTED_REASONS`, or
    a ``spawn_refused`` mark (``gate_runner``'s own word for a launch that did
    not happen). Everything else may be a run that started, so it is booked —
    as an unknown when its usage is missing, never silently dropped.
    """
    if canonical_status(dict(record)) == "not_executable":
        return True
    if record.get("spawn_refused"):
        return True
    return str(record.get("reason") or "").strip() in _NEVER_STARTED_REASONS


def _gate_attempt_identity(record: Mapping[str, Any], gate: str) -> Optional[Tuple[str, str]]:
    """``(provider, model)`` as the runner stamped them, or from the registry, or ``None``.

    The identity is the one the RUN held: ``provider``/``model`` land on the
    record when the runner stamped them (``gate_artifacts._stamp_run_settings``,
    ``governed_lane._build_record``, ``gate_recorder._failure_payload``). A
    record that carries none — a ``not_executable`` gate, a record written
    before the stamp existed — falls back to the registered provider and the
    gate's own model resolution (``gate_lane_contract.gate_model``), which is
    the identity the runner WOULD have launched (ADR-036). A provider outside
    the model lanes (``ci_gate``, ``wiring_gate``, a ``gh`` read, a gemini PATH
    binary) names no model run: ``None``, no line. The model is never read from
    the model's own report text.
    """
    import gate_lane_contract  # noqa: PLC0415 — sibling module
    import harness_run_cost  # noqa: PLC0415 — sibling module

    provider_row = gate_recorder.resolve_gate_provider(gate)
    provider = str(record.get("provider") or "").strip()
    model = str(record.get("model") or "").strip()
    if not provider:
        if provider_row is None:
            return None
        provider = provider_row[1]
    if provider not in harness_run_cost.LANE_BILLING:
        return None
    if not model:
        model = (gate_lane_contract.gate_model(gate) or "").strip()
    if not model:
        return None
    return provider, model


def _attempt_window(record: Mapping[str, Any]) -> Tuple[datetime, datetime]:
    """``(started, ended)`` of the run this record ends, from its own timestamps and duration."""
    ended = datetime.now(timezone.utc)
    recorded = str(record.get("recorded_at") or record.get("completed_at") or "").strip()
    if recorded:
        try:
            moment = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
        except ValueError:
            moment = None
        if moment is not None and moment.tzinfo is not None:
            ended = moment
    started = ended
    duration = record.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        started = ended - timedelta(seconds=float(duration))
    return started, ended


def _attempt_ref(record: Mapping[str, Any], pr_ref: str) -> str:
    """The ref the booking is logged under: the run's dispatch id, else the PR it reviewed."""
    dispatch_id = str(record.get("dispatch_id") or "").strip()
    if dispatch_id:
        return dispatch_id
    number = record.get("pr_number")
    if isinstance(number, int) and not isinstance(number, bool):
        return f"pr-{number}"
    return pr_ref or "pr-unknown"


def _book_gate_attempt(*, gate: str, record: Mapping[str, Any], result_path: Path, pr_ref: str) -> None:
    """Book the one ledger line of the execution this gate outcome ends (K3-2b), or none.

    The identity is the record's own ``execution_id``, minted once before the
    run's first model call (K3-2a): the SAME execution committed again finds its
    line and writes nothing (:func:`cost_ledger.record_once`), and a separate
    run whose record the guard refused books its own. ``attempt_seq`` is never
    read — it numbers slot writes, and the ledger numbers executions.

    Four records book nothing, each for its own reason: ``--reprocess`` evidence
    (it rebuilt a report an earlier run wrote and called no model); a run that
    provably never started (:func:`_never_started` — a refused retry carries no
    id by design, and that is not an anomaly); an execution K2b already booked
    at its lane's own measured end (``record_once`` finds that line by its
    ``execution_id``); and a record that names no model run (``ci_gate``, a
    ``gh`` read).

    A run that DID start and still names no valid id books nothing either, but
    never silently: one WARNING naming the dispatch and the count
    ``harness_run_cost.UNBOOKED_NO_EXECUTION_ID``. No identity is ever guessed.

    Fail-open by construction: ``record_attempt_run`` logs its own refusals and
    returns them, so a broken ledger leaves the verdict, its receipt, the
    request end and this caller exactly as they were.
    """
    if record.get(EVIDENCE_SOURCE_FIELD) == EVIDENCE_SOURCE_REPROCESSED:
        logger.debug("gate_outcome: %s at %s is reprocessed evidence; no cost line", gate, result_path)
        return
    if _never_started(record):
        logger.debug("gate_outcome: %s at %s names a run that never started; no cost line", gate, result_path)
        return
    state_dir = gate_recorder._state_dir_from_result_path(result_path)
    if state_dir is None:
        # An ad-hoc fixture (or a caller writing straight into a tmp dir) has no
        # store to book into. Production result paths always do.
        logger.debug("gate_outcome: %s at %s has no store; no cost line", gate, result_path)
        return
    identity = _gate_attempt_identity(record, gate)
    if identity is None:
        logger.debug("gate_outcome: %s at %s names no model run; no cost line", gate, result_path)
        return
    execution_id = record.get(EXECUTION_ID_FIELD)
    if not is_execution_id(execution_id):
        _count_unbooked(gate=gate, record=record, pr_ref=pr_ref, result_path=result_path)
        return
    import provider_costs  # noqa: PLC0415 — sibling module
    import harness_run_cost  # noqa: PLC0415 — sibling module

    provider, model = identity
    started, ended = _attempt_window(record)
    harness_run_cost.record_attempt_run(
        harness_run_cost.AttemptRun(
            provider=provider, model=model, execution_id=execution_id,
            started=started, ended=ended,
        ),
        project_id=harness_run_cost.ledger_project_id(state_dir),
        producer=gate, ref=_attempt_ref(record, pr_ref), role=harness_run_cost.GATE_ROLE,
        # The store this record landed in, never the ambient env.
        ledger_path=provider_costs.costs_path_in(state_dir.parent),
    )


def _count_unbooked(*, gate: str, record: Mapping[str, Any], pr_ref: str, result_path: Path) -> None:
    """One WARNING and one count for a run that started and names no execution id (K3-2b).

    Never silent and never guessed: without an id there is no line that a
    re-commit can find, so the honest outcome is no line at all, said out loud
    with the dispatch id a reader can go and look at.
    """
    import harness_run_cost  # noqa: PLC0415 — sibling module

    harness_run_cost.count_booking_refusal(harness_run_cost.UNBOOKED_NO_EXECUTION_ID)
    logger.warning(
        "gate_outcome: %s pr=%s dispatch=%s head=%s at %s committed WITHOUT a valid execution id "
        "(%r): no cost line booked, counted %s",
        gate, pr_ref, record.get("dispatch_id") or "?", record.get("commit_sha") or "?",
        result_path, record.get(EXECUTION_ID_FIELD), harness_run_cost.UNBOOKED_NO_EXECUTION_ID,
    )


def _pay_outstanding_receipt(path: Path, existing: Any, *, gate: str, pr_ref: str) -> None:
    """Step 3: book the receipt the record on the slot still owes, or refuse to replace it."""
    if not (isinstance(existing, dict) and existing.get("receipt_pending") is True):
        return
    # Only this writer sets ``receipt_pending``, and it sets it on an undecided
    # record only through GE-8's ``book_undecided_receipt`` hook. So a pending
    # undecided record IS such a debt: paying it must book the receipt rather
    # than return early and then clear the flag with nothing booked.
    if _book_receipt(
        path, existing, gate=gate, pr_ref=pr_ref,
        book_undecided=not gate_recorder.owes_outcome_receipt(existing),
    ):
        return
    raise ReceiptPendingRefused(
        f"{gate} result for pr={pr_ref!r} at {path}: the record on the slot "
        f"(commit_sha={existing.get('commit_sha')!r}, status={existing.get('status')!r}, "
        f"attempt_seq={existing.get('attempt_seq')!r}) still owes its review_gate_result "
        f"receipt and booking it failed again; refusing to replace it until that receipt "
        f"is booked (ADR-046 §3)"
    )


def _pending_obligation(path: Path, existing: Any) -> Optional[GateObligation]:
    """The obligation booking the record on the slot still owes, or ``None`` (step 8).

    Only ``obligation_pending is True`` is a debt: this writer sets it and
    nothing else does. A ``True`` whose booking cannot be read is a refusal,
    not an empty work order — replacing the record would delete the only trace.
    """
    if not (isinstance(existing, dict) and existing.get(OBLIGATION_PENDING_KEY) is True):
        return None
    booking = existing.get(OBLIGATION_BOOKING_KEY)
    target = booking.get("path") if isinstance(booking, dict) else None
    fields = booking.get("fields") if isinstance(booking, dict) else None
    if not (isinstance(target, str) and target.strip() and isinstance(fields, dict)):
        raise ObligationPendingRefused(
            f"the record at {path} owes an obligation booking but its {OBLIGATION_BOOKING_KEY} "
            f"is unreadable ({booking!r}); refusing to replace it (ADR-046 §2 step 8)"
        )
    return GateObligation(path=Path(target), fields=fields)


def _pay_outstanding_obligation(path: Path, existing: Any) -> None:
    """Step 8: book the obligation the record on the slot still owes, or refuse to replace it."""
    obligation = _pending_obligation(path, existing)
    if obligation is None:
        return
    if _book_obligation(path, existing, obligation) is not None:
        return
    raise ObligationPendingRefused(
        f"{obligation.path} at {path}: the record on the slot "
        f"(commit_sha={existing.get('commit_sha')!r}, attempt_seq={existing.get('attempt_seq')!r}) "
        f"still owes its obligation booking and booking it failed again; refusing to replace it "
        f"until that booking lands (ADR-046 §2 step 8)"
    )


def _stamp_obligation_work_order(record: Dict[str, Any], obligation: GateObligation) -> None:
    """Make ``record`` a durable work order for ``obligation`` (step 8): the two keys, nothing else."""
    record[OBLIGATION_PENDING_KEY] = True
    record[OBLIGATION_BOOKING_KEY] = {"path": str(obligation.path), "fields": dict(obligation.fields)}


def _persist_obligation_work_order(
    path: Path, record: Dict[str, Any], obligation: Optional[GateObligation],
) -> None:
    """Step 8 on the REPEAT path: stamp the booking on the record already on the slot, then write it.

    A repeat writes no record of its own, so the work order has to be put
    there before the booking is tried: a booking that raises otherwise leaves
    nothing on disk, and the obligation is lost while the commit returns
    success. A write that fails here raises BEFORE the booking is attempted,
    so no commit returns with neither a booking nor a work order.
    """
    if obligation is None:
        return
    _stamp_obligation_work_order(record, obligation)
    gate_recorder._write_result_atomic(path, record)


def _obligation_attempt(record: Mapping[str, Any]) -> Dict[str, Any]:
    """The attempt identity a booking is stamped with: absent/malformed values become ``""``/``None``."""
    head = record.get("commit_sha")
    seq = record.get("attempt_seq")
    return {
        "commit_sha": head if isinstance(head, str) else "",
        "attempt_seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
    }


def _book_obligation(
    path: Path, record: Dict[str, Any], obligation: GateObligation, *, raise_on_failure: bool = False,
) -> Optional[Dict[str, Any]]:
    """Book the obligation, then clear the record's pending flag; a failed booking leaves the flag.

    The caller holds the record slot's lock. Returns the obligation record, or
    ``None`` when the booking failed and the flag stays as the next commit's
    work order. ``raise_on_failure`` is the obligation-only path's: no record
    was written there, so the failure has no flag to hang on.
    """
    try:
        booked = gate_obligations.book_obligation_outcome(
            obligation.path, dict(obligation.fields), attempt=_obligation_attempt(record),
        )
    # vnx-broad-except: an obligation store that cannot be written or read fails
    # this step only — the record and receipt already landed, and the flag stays
    # as the durable work order (the obligation-only path raises instead).
    except (OSError, ValueError) as exc:
        if raise_on_failure:
            raise
        logger.warning(
            "gate_outcome: obligation %s NOT booked for head=%s attempt=%s — %s: %s. "
            "The record keeps obligation_pending; the next commit on this slot books it.",
            obligation.path, record.get("commit_sha"), record.get("attempt_seq"), type(exc).__name__, exc,
        )
        return None
    _clear_obligation_flag(path, record)
    return booked


def _clear_obligation_flag(path: Path, record: Dict[str, Any]) -> None:
    """Clear the pending booking with one atomic write; a record that owes none is not rewritten."""
    if OBLIGATION_PENDING_KEY not in record and OBLIGATION_BOOKING_KEY not in record:
        return
    booking = record.pop(OBLIGATION_BOOKING_KEY, None)
    record.pop(OBLIGATION_PENDING_KEY, None)
    try:
        gate_recorder._write_result_atomic(path, record)
    except OSError as exc:
        # Booked, but the flag stays: the next commit books it again, and the
        # booking's value equality makes that a no-op.
        record[OBLIGATION_PENDING_KEY] = True
        if booking is not None:
            record[OBLIGATION_BOOKING_KEY] = booking
        logger.warning(
            "gate_outcome: obligation booked for %s, obligation_pending not cleared — %s: %s",
            path, type(exc).__name__, exc,
        )


def commit_obligation_outcome(
    obligation: GateObligation, *, result_path: Path,
) -> Dict[str, Any]:
    """Book one obligation outcome under the record slot's lock, rewriting no record and no receipt.

    The obligation half of :func:`commit_gate_outcome`, for callers that decide
    the booking after the gate's run committed (``gate_obligation_runner``): it
    lands inside that slot's lock, pays a debt the record still owes first, and
    never invents a second ``attempt_seq``. A failed booking raises
    (OSError/ValueError): this path has no fresh record to hang a flag on.
    """
    with slot_lock(result_path):
        existing = gate_recorder._read_existing_result(result_path)
        record = existing if isinstance(existing, dict) else {}
        _pay_outstanding_obligation(result_path, existing)
        booked = _book_obligation(result_path, record, obligation, raise_on_failure=True)
    return booked or {}  # ``raise_on_failure``: a booking that did not land never reaches here


def _repeats_the_slot(existing: Any, record: Dict[str, Any], *, undecided_repeats: bool = False) -> bool:
    """Step 6: does this write repeat the outcome already on the slot?

    Positive evidence only: both records owe a receipt, both name the same
    non-empty head, status and pass/fail decision are equal (the receipt
    books ``is_pass``, so a ``completed`` with blocking findings is a fail),
    and so is the evidence (:func:`_changed_evidence`). Anything missing or
    malformed is no repeat, and the write goes ahead.

    ``undecided_repeats`` (GE-8) is the opt-in of the writer that books a
    receipt for an UNDECIDED record too (``book_undecided_receipt``). Such a
    record's receipt is already booked when the slot holds the same answer, so
    rewriting it would advance ``attempt_seq`` while the key returned that
    receipt as a duplicate — and a commit that only has to finish a request
    (the orphan cleanup's recovery) would write a second record for a first one
    it never changed. Both sides must then be undecided, and the records must
    be the same answer (:func:`_same_undecided_record`).
    """
    if not isinstance(existing, dict):
        return False
    if gate_recorder.owes_outcome_receipt(existing) and gate_recorder.owes_outcome_receipt(record):
        old_head, new_head = existing.get("commit_sha"), record.get("commit_sha")
        if not (isinstance(old_head, str) and isinstance(new_head, str) and old_head.strip()):
            return False
        return (old_head.strip() == new_head.strip()
                and canonical_status(existing) == canonical_status(record)
                and is_pass(existing)[0] == is_pass(record)[0]
                and not _changed_evidence(existing, record))
    if not undecided_repeats:
        return False
    if gate_recorder.owes_outcome_receipt(existing) or gate_recorder.owes_outcome_receipt(record):
        return False
    return _same_undecided_record(existing, record)


def _same_undecided_record(existing: Dict[str, Any], record: Dict[str, Any]) -> bool:
    """Step 6 (GE-8): is the undecided record on the slot already this very answer?

    Every key either side names, other than the ones that record when and how
    often the record was written (:data:`_VOLATILE_RECORD_KEYS`), must be
    present on both sides and equal. A key only one side names is a difference,
    like a differing value: absent is not "unset on both", and this decides a
    write rather than a display. Both sides must also name the same non-empty
    status — a record with none is not evidence that anything is already booked.
    """
    status = canonical_status(existing)
    if not status or status != canonical_status(record):
        return False
    for name in set(existing) | set(record):
        if name in _VOLATILE_RECORD_KEYS:
            continue
        if name not in existing or name not in record or existing[name] != record[name]:
            return False
    return True


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
    receipt_fields: Mapping[str, Any], obligation: Optional[GateObligation] = None,
    book_undecided_receipt: bool = False,
) -> None:
    """Steps 1, 2, 4 and 8's work order; the caller holds the slot's lock and the guard admitted the write.

    The pending obligation booking is stamped on the record BEFORE the atomic
    write, next to ``receipt_pending``: a process that dies between the record
    and the obligation leaves the same durable work order (step 8).
    ``book_undecided_receipt`` is GE-8's opt-in: an ``unavailable`` record
    stamped here carries a receipt debt like a decided one.
    """
    seq = existing.get("attempt_seq") if isinstance(existing, dict) else None
    record["attempt_seq"] = seq + 1 if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1 else 1
    owes_receipt = gate_recorder.owes_outcome_receipt(record) or book_undecided_receipt
    if owes_receipt:
        record["receipt_pending"] = True
        if receipt_fields:
            record["receipt_fields"] = dict(receipt_fields)
    else:
        record.pop("receipt_pending", None)
    if obligation is not None:
        _stamp_obligation_work_order(record, obligation)
    gate_recorder._write_result_atomic(path, record)
    if owes_receipt:
        _book_receipt(
            path, record, gate=gate, pr_ref=pr_ref, book_undecided=book_undecided_receipt,
        )


def _finish_request(request: GateRequest, record: Dict[str, Any], outcome: GateOutcome) -> Optional[RequestWriteFailed]:
    """Step 7; the caller holds the slot's lock and the record on the slot answers the request.

    The ended request is built as a COPY of the caller's payload, written back
    only once the write landed (kimi advisory (b)); a failed write comes back as
    :class:`RequestWriteFailed` and the commit raises it after the archive.
    """
    status = REQUEST_STATUS_OF.get(canonical_status(record))
    if status is None:
        logger.warning("gate_outcome: %s request pr=%s left as it was: status %r ends no request",
                       outcome.gate, outcome.pr_ref, record.get("status"))
        return None
    status = request.status or status
    ended = {**request.payload, **request.fields, "status": status}
    try:
        gate_recorder.persist_request(
            request.requests_dir, outcome.gate, ended, pr_number=outcome.pr_number, pr_id=outcome.pr_id,
        )
    except OSError as exc:
        return RequestWriteFailed(
            f"{outcome.gate} request for pr={outcome.pr_ref!r} NOT set to {status!r}: the record "
            f"and its receipt landed, the request write failed — {type(exc).__name__}: {exc}",
            record=record, status=status,
        )
    request.payload.update(ended)
    return None


def _named(value: Any) -> bool:
    """Positive evidence: a value that names something — not ``None``, not an empty or blank string."""
    if value is None:
        return False
    return not (isinstance(value, str) and not value.strip())


def _foreign_request_reason(
    on_disk: gate_recorder.RequestOnDisk, held: Mapping[str, Any], *, gate: str,
) -> Optional[Tuple[str, str]]:
    """Step 9: why the request on the slot is NOT the one the caller holds, or ``None`` when it is.

    Positive evidence only, in this order: the slot must hold a request at all,
    that request must still be in flight, and every identity field either side
    names (:data:`REQUEST_IDENTITY_FIELDS`) must be named on BOTH sides and be
    equal. Absent, unreadable, already terminal and mismatched all leave the
    record untouched — none of them is evidence that this write owns it.

    Only the KNOWN terminal statuses (:data:`REQUEST_TERMINAL_STATUSES`) refuse
    on the status: an unknown, empty or missing one is not read as "already
    ended", because a status this code does not recognise is not evidence about
    the request, and letting it block the write would leave the caller's own
    in-flight request `executing` for ever — the re-run loop step 9 exists to
    close. Identity, which can be verified, is what decides.
    """
    if not on_disk.present:
        return REQUEST_NOT_ON_DISK, f"no request record at {on_disk.path}"
    current = on_disk.record
    if not isinstance(current, dict):
        return REQUEST_UNREADABLE, f"the request at {on_disk.path} cannot be read as an object"
    status = current.get("status")
    if status in REQUEST_TERMINAL_STATUSES:
        return REQUEST_ALREADY_ENDED, f"the request at {on_disk.path} already ended {status!r}"
    for name in REQUEST_IDENTITY_FIELDS:
        # The gate is the caller's own argument, not what its payload claims:
        # the slot path this write targets was built from it.
        mine, theirs = (gate if name == "gate" else held.get(name)), current.get(name)
        if not (_named(mine) or _named(theirs)):
            continue  # neither side names it: it identifies nothing and so decides nothing
        if not (_named(mine) and _named(theirs)) or mine != theirs:
            return REQUEST_ANOTHER_ATTEMPT, f"{name!r} differs ({theirs!r} on the slot, {mine!r} held)"
    return None


def _finish_refused_request(
    request: GateRequest, refusal: gate_recorder.ResultOverwriteRefused, *,
    gate: str, pr_number: Optional[int], pr_id: str,
) -> Optional[Union[RequestWriteFailed, RequestLeftAlone]]:
    """Step 9: end a refused run's request, so it is never re-run into the refusal.

    ``failed`` + ``failure_reason: write_refused`` when the writer named no end
    (the refused ci_gate write used to stay ``executing`` and be re-run for
    ever), and the writer's OWN end when it named one — ``record_failure``'s
    ``failed``/``unavailable``, ``record_not_executable``'s ``not_executable``:
    OI-1707 measured that a runner's diagnosis must not be displaced. All of
    them are terminal, so ``execute_gate`` short-circuits and a real retry is a
    NEW request.

    GE-4c round 2: the request ENDED is the one the caller holds, decided from
    the record on the slot under that slot's lock (:func:`_foreign_request_reason`),
    never from the caller's copy — the caller's ``payload`` is what it read
    before the gate ran, and another writer may have ended or replaced that
    record since. A request that is already terminal, or that belongs to another
    attempt (a newer retry in the same ``(gate, PR)`` slot), is returned as
    :class:`RequestLeftAlone` and not touched: overwriting it would delete the
    completion metadata of a run that finished, or end a retry this writer never
    ran.
    """
    named = request.status if request.status in REQUEST_TERMINAL_STATUSES else ""
    if named:
        # The writer named the end and carries its diagnosis in ``fields``
        # (reason/reason_detail, or the run's own time): a refused write must
        # not drop either (OI-1707 measured that).
        status = named
        ended = {**request.payload, **request.fields, "status": named}
    else:
        # No end of its own: the attempt ends here, and it ends unrecorded.
        status = "failed"
        ended = {**request.payload, "status": status, "failure_reason": WRITE_REFUSED}
    held = request.payload
    try:
        refused = gate_recorder.persist_request(
            request.requests_dir, gate, ended, pr_number=pr_number, pr_id=pr_id,
            refuse_if=lambda on_disk: _foreign_request_reason(on_disk, held, gate=gate),
        )
    except OSError as exc:
        return RequestWriteFailed(
            f"{gate} request for a refused write could not be ended {status!r}: "
            f"{type(exc).__name__}: {exc} (refusal: {refusal})",
            record=request.payload, status=status,
        )
    if refused is not None:
        logger.warning(
            "gate_outcome: %s request for pr=%s left as it is — %s (%s); a refused write ends "
            "only the request it holds (ADR-046 §2 step 9)",
            gate, pr_id or pr_number, refused.reason, refused.detail,
        )
        return RequestLeftAlone(reason=refused.reason, detail=refused.detail)
    request.payload.update(ended)
    return None


def _book_receipt(
    path: Path, record: Dict[str, Any], *, gate: str, pr_ref: str, book_undecided: bool = False,
) -> bool:
    """Book the record's receipt, then clear its flag; a failed append leaves the flag.

    Only ``receipt_pending is True`` is a debt (the caller checks): a missing,
    ``None`` or other value was not set by this writer and is left alone.
    Returns ``False`` only when the append raised. ``True`` means nothing is
    owed any more: the append returned (appended or duplicate), or the record
    owes no receipt; a failed clearing write afterwards does not change that.
    The writer's own ``receipt_fields`` travel with the flag.
    ``book_undecided`` (GE-8) books the receipt of an ``unavailable`` record;
    the caller that sets it is the only one that stamped such a debt.
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
            book_undecided=book_undecided,
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
