"""contract_invalid_ledger.py — visibility and non-silent-closure gating for
contract_invalid receipts (OI-1638).

Measured 05-09 (claudedocs/2026-09-05-golf2-contractpercentage-per-lane.md
§5a): envelope_govern.py's report-body-contract check works — it correctly
stamps 350+ receipts as ``status: "contract_invalid"`` in t0_receipts.ndjson
when ``validate_body()`` rejects a worker's report. But before this module,
every existing reader of that status (weekly_digest.py, learning_loop.py,
check_active_drain.py, receipt_classifier.py, event_outcome_semantics.py,
receipt_verdict.py, router_baseline.py, stop_conditions.py,
deliberation_panel.py — measured via ``grep -rn contract_invalid scripts/``,
NOT "only writers" as first assumed) treats it only as one more failure
bucket for a digest, a drain check, or a routing decision. None of them turn
a single contract_invalid receipt into a durable, per-dispatch consequence:
no open item, no restart, no block, no visible count anywhere a human looks
at session start. This module is the missing read side:

  - ``build_contract_invalid_summary``: the counters build_t0_state.py
    surfaces at every SessionStart (gevolg 1).
  - ``collect_contract_invalid_open`` / ``write_contract_invalid_open_ledger``:
    the single-file open-items ledger — one row per still-open dispatch, not
    one open item per receipt (gevolg 2, "een lijst, geen vloed").
  - ``is_deliverable_acceptable`` / ``evaluate_deliverable_acceptance``: the
    gate a closer must call before treating a dispatch's deliverable as done
    (gevolg 3, "niet stil sluitbaar"). Its call site is the merge door
    (``pr_merge.py``'s ``_run_contract_invalid_gate``). The first returns
    ``(bool, prose)``; the second returns that same judgment plus the
    machine-readable ``code`` a caller may DECIDE on (golf Bx, D4 / OI-1666 —
    the door's override covers one of the three refusals, and prose is not a
    contract).

Which receipts that gate may judge is itself the thing that decides whether
it fires at all, and it takes TWO filters to get there:

  - WHICH PLANE. Every plane writes on the SAME dispatch_id — the worker's
    outcome, the review gate's request, state mutations — so "the latest
    receipt for this dispatch" is normally a gate-plane record, not the
    deliverable. Measured 07-09 over the live ledger: all 8 dispatch-ids
    with a contract_invalid receipt before their ``pr_merged`` had a
    ``review_gate_request`` in between, so the unfiltered version passed all
    8. ``DELIVERABLE_OUTCOME_EVENT_TYPES`` is that filter.
  - WHETHER IT DECIDES ANYTHING. An outcome receipt whose status says
    nothing about the deliverable (``unknown``, ``no_signal``, empty) must
    not lift an earlier refusal just by being later. It did: the plane
    filter alone still passed 2 of those 8.
    ``DECIDED_OUTCOME_STATUSES`` is that filter and carries the counts.

Staleness windowing is delegated to the existing
``contract_invalid_window.is_stale_contract_invalid`` / the effective
timestamp it defines — never reimplemented here. The VERDICT itself, both
for "open" and for "acceptable", is still read purely from the
``contract_invalid`` status/event_type literal (``_is_contract_invalid``) —
the two filters above only decide WHICH receipt that literal is read from.
So no success/failure classification is needed, and this module does not
delegate to ``event_outcome_semantics.classify_event_outcome``; where its
judgment is relevant (``task_complete``/``unknown`` classifying as neither),
it is cited as corroboration, never called.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from atomic_io import atomic_write_json  # noqa: E402
from contract_invalid_window import contract_invalid_effective_timestamp  # noqa: E402

CONTRACT_INVALID_STATUS = "contract_invalid"
CONTRACT_INVALID_EVENT_TYPE = "report_contract_invalid"

OPEN_LEDGER_FILENAME = "contract_invalid_open.json"

# Receipts that carry a DELIVERABLE OUTCOME for a dispatch — the only ones
# ``is_deliverable_acceptable`` may judge on.
#
# Measured 07-09 over the live ledger (29.386 records,
# ~/.vnx-data/vnx-dev/state/t0_receipts.ndjson): the event types that ever
# carry the contract_invalid literal are ``report_contract_invalid`` (3323),
# ``task_complete`` (35) and ``subprocess_completion`` (4). ``task_failed``
# is in this set as the fourth deliverable outcome (a governed failure
# resolves a chain just as a governed success does) even though it never
# carries the literal itself.
#
# Everything else on a dispatch_id belongs to the GATE or STATE plane and
# says nothing about the deliverable: ``review_gate_request`` alone has 844
# records and is written by gate_request_handler.py on the SAME dispatch_id
# AFTER the worker's outcome receipt. Judging "the latest receipt" without
# this filter therefore reads the gate request, not the deliverable. Measured
# on the same ledger: of the 8 dispatch-ids that had a contract_invalid
# receipt before their pr_merged, ALL 8 had a ``review_gate_request`` as
# their last receipt before the merge — the gate would have said GO on every
# single one.
#
# What this filter alone did NOT fix, measured on the same 8: it refuses 6.
# The other 2 are what ``DECIDED_OUTCOME_STATUSES`` below is for.
DELIVERABLE_OUTCOME_EVENT_TYPES = frozenset({
    "report_contract_invalid",
    "task_complete",
    "subprocess_completion",
    "task_failed",
})

# Outcome statuses that actually DECIDE the deliverable — the only ones that
# may be chosen as "the latest outcome receipt".
#
# Measured 07-09 on the live ledger (29.388 records): the plane filter above
# refuses 6 of the 8 dispatch-ids that carried a contract_invalid receipt
# before their merge. The remaining 2 share one chain shape:
#
#   20260830-140500-d6a2-kopie-erfde-de-violation
#     task_complete  contract_invalid  effective 2026-08-30T12:11:42Z
#     task_complete  unknown           effective 2026-08-30T12:12:59Z
#   20260830-145100-d5-sync-main-na-1732 : identical, 12:44:47 → 12:46:05
#
# The ordering is right (``contract_invalid_effective_timestamp`` prefers
# ``ingested_at``, and that ``unknown`` receipt really is later). The defect
# is that "not the contract_invalid literal" was read as "resolved": an
# ``unknown`` says nothing about the deliverable, and
# ``event_outcome_semantics.classify_event_outcome("task_complete",
# "unknown")`` agrees — it returns None, neither success nor failure. Only a
# receipt that decides may overturn a governed refusal, so an undecided one
# is SKIPPED at selection time, exactly as a gate-plane receipt already is.
#
# Status distribution over the 23.698 deliverable-outcome receipts in that
# ledger: success 9130, failed 4279, unknown 3897, contract_invalid 3362,
# done 2405, failure 352, empty 134, timeout 94, no_signal 35, complete 4,
# blocked 2, completed 1, in_progress 1, and 2 free-text worker statuses
# ("done — awaiting ci + t0 gate", "complete — push + pr created").
#
# This is an ALLOWLIST, not a blocklist of {unknown, no_signal, empty}: the
# writers of this field are not a closed set (those last 6 records prove it),
# and a status literal this module has never seen makes no statement about
# the deliverable either. Under a blocklist any new literal would silently
# heal a refused chain; under an allowlist it fails closed and the operator
# still has ``--override-contract-invalid <reden>``. A contract_invalid
# receipt is never skipped by this filter regardless of its status literal —
# ``_is_decided_outcome`` short-circuits on ``_is_contract_invalid`` first,
# so an old ``report_contract_invalid`` record with no status field cannot
# drop out of its own check.
DECIDED_OUTCOME_STATUSES = frozenset({
    "success",
    "done",
    "complete",
    "failed",
    "failure",
    "timeout",
    CONTRACT_INVALID_STATUS,
})

_MIN_AWARE_DATETIME = datetime.min.replace(tzinfo=timezone.utc)

# ── Acceptance reason codes (golf Bx, D4 / OI-1666) ───────────────────────
#
# ``is_deliverable_acceptable`` answered with a bool plus a Dutch prose
# reason, and that prose was the ONLY thing distinguishing its three refusals
# from one another. The merge door's ``--override-contract-invalid`` therefore
# covered all three: an operator who meant "I know about that
# contract_invalid" silently also waved through "I cannot read the ledger",
# where repairing is the right move and proceeding is not.
#
# These codes are what a caller DECIDES on; the reason string stays a message
# for a human and may be reworded freely without moving a gate. Matching on
# that prose instead is the exact failure class golf Bx exists to remove — it
# breaks on the first rewrite, silently and in the permissive direction.
#
# Deliberately plain string constants rather than an ``enum.Enum``: this
# module already carries its vocabularies this way
# (``CONTRACT_INVALID_STATUS``, ``DECIDED_OUTCOME_STATUSES``), the values are
# written straight into JSON gate results, and a ``(str, Enum)`` member
# formats as ``AcceptanceCode.X`` in an f-string on Python 3.11+ but as its
# value on 3.10 — a version-dependent difference that has no place in an
# audit message.

#: Refusal: the latest decided deliverable-outcome receipt IS contract_invalid.
#: The ONE case ``--override-contract-invalid`` may bypass. Its VALUE equals
#: ``CONTRACT_INVALID_STATUS`` because it names the same fact from the other
#: side (a receipt status vs. why this gate refused); they are separate
#: constants and a caller compares a code against codes, never against a
#: receipt's ``status`` field.
CODE_CONTRACT_INVALID = "contract_invalid"

#: Refusal: the ledger exists but could not be read (``strict=True``). A gate
#: outage to be repaired, never a judgment about the deliverable.
CODE_LEDGER_UNREADABLE = "ledger_unreadable"

#: Refusal: no dispatch_id was given, so there is no chain to judge at all.
CODE_EMPTY_DISPATCH_ID = "empty_dispatch_id"

#: Accepted: a decided outcome receipt exists and is not contract_invalid.
CODE_NOT_CONTRACT_INVALID = "not_contract_invalid"

#: Accepted (fail-open absence): no deliverable-outcome receipt on record.
CODE_NO_OUTCOME_RECEIPT = "no_outcome_receipt"

#: Accepted (fail-open absence): outcome receipts exist but none of them
#: decides the deliverable.
CODE_NO_DECIDED_OUTCOME = "no_decided_outcome"

#: The closed set of codes this module returns. A reader may switch on a code
#: from this set; anything outside it is a bug in this module, not a case to
#: handle permissively — a consumer meeting an unknown code must fail closed.
ACCEPTANCE_CODES = frozenset({
    CODE_CONTRACT_INVALID,
    CODE_LEDGER_UNREADABLE,
    CODE_EMPTY_DISPATCH_ID,
    CODE_NOT_CONTRACT_INVALID,
    CODE_NO_OUTCOME_RECEIPT,
    CODE_NO_DECIDED_OUTCOME,
})

#: The refusing subset, for a caller that wants to assert it handled them all.
REFUSING_ACCEPTANCE_CODES = frozenset({
    CODE_CONTRACT_INVALID,
    CODE_LEDGER_UNREADABLE,
    CODE_EMPTY_DISPATCH_ID,
})


class DeliverableAcceptance(NamedTuple):
    """The verdict of ``evaluate_deliverable_acceptance``: the boolean, the
    machine-readable ``code`` a caller decides on, and the human ``reason``.

    A NamedTuple rather than a widened return tuple on
    ``is_deliverable_acceptable``: that function is a gate function with
    call sites outside this module (``pr_merge.py``) and ~20 in the suite,
    all unpacking two values. Widening it to three would have broken every
    one of them at once, so the widening is ADDITIVE — the two-tuple contract
    stays exactly as it was and is still tested, and the coded form is a
    second entry point that returns the same judgment with its cause attached.
    """

    acceptable: bool
    code: str
    reason: str


class ReceiptsUnreadableError(OSError):
    """The receipts ledger exists but could not be read.

    Raised by ``_read_receipts_strict`` and surfaced to the merge door so an
    I/O failure becomes a refusal with its cause, never an empty read that
    looks like "no receipt on record" (which fails OPEN). The advisory
    readers keep the soft ``_read_receipts`` and never see this.
    """


def _parse_ts(value: Optional[Any]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z`` and naive
    values (assumed UTC). Returns ``None`` on absence or malformed input —
    mirrors ``contract_invalid_window._parse_timestamp`` (not imported: that
    helper is private to its module) so every caller here gets an aware
    datetime or ``None``, never a naive/aware comparison crash.
    """
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_contract_invalid(record: Dict[str, Any]) -> bool:
    status = str(record.get("status") or "").strip().lower()
    event_type = str(record.get("event_type") or record.get("event") or "").strip().lower()
    return status == CONTRACT_INVALID_STATUS or event_type == CONTRACT_INVALID_EVENT_TYPE


def _is_outcome_receipt(record: Dict[str, Any]) -> bool:
    """True when this receipt reports a deliverable outcome for its dispatch.

    Decided on the event type alone (``DELIVERABLE_OUTCOME_EVENT_TYPES``), not
    on the status: a gate-plane receipt is not a statement about the
    deliverable regardless of what status it happens to carry.
    """
    event_type = str(record.get("event_type") or record.get("event") or "").strip().lower()
    return event_type in DELIVERABLE_OUTCOME_EVENT_TYPES


def _is_decided_outcome(record: Dict[str, Any]) -> bool:
    """True when this outcome receipt DECIDES the deliverable one way or the
    other (``DECIDED_OUTCOME_STATUSES``) — see that constant for the measured
    reason and for why it is an allowlist.

    A contract_invalid receipt is decided by definition and is checked first,
    so it is never skipped over its status literal (an old
    ``report_contract_invalid`` record carrying no status field at all still
    counts).
    """
    if _is_contract_invalid(record):
        return True
    status = str(record.get("status") or "").strip().lower()
    return status in DECIDED_OUTCOME_STATUSES


def _read_receipts_strict(receipts_path: Path) -> List[Dict[str, Any]]:
    """Read every well-formed JSON object line from an NDJSON receipts file,
    raising ``ReceiptsUnreadableError`` when the file exists but cannot be read.

    A file that does not exist is a legitimate absence (a fresh store, an
    isolated state dir) and yields ``[]`` here too — only a real I/O failure
    on an existing path is an error. Malformed individual lines are still
    skipped: one corrupt line must not blind the reader to the other 29.385.
    """
    if not receipts_path.exists():
        return []
    try:
        text = receipts_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise ReceiptsUnreadableError(f"{receipts_path}: {exc}") from exc
    records: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _read_receipts(receipts_path: Path) -> List[Dict[str, Any]]:
    """Soft read for the ADVISORY readers: absence, an unreadable file, and
    malformed individual lines all degrade to being skipped rather than
    raising — a SessionStart counter must not crash a session over a
    permission bit.

    The merge gate does NOT use this: see ``_read_receipts_strict`` /
    ``is_deliverable_acceptable(strict=True)``, where the same I/O failure
    must become a refusal instead of an empty read.
    """
    try:
        return _read_receipts_strict(receipts_path)
    except ReceiptsUnreadableError:
        return []


def _filter_project(records: List[Dict[str, Any]], project_id: Optional[str]) -> List[Dict[str, Any]]:
    """Scope records to ``project_id`` when given. A record without its own
    project_id passes through (backward compat with pre-tenant-stamping
    receipts) — mirrors the same tolerance in build_t0_state._build_queues.
    """
    pid = (project_id or "").strip()
    if not pid:
        return records
    out = []
    for r in records:
        r_pid = str(r.get("project_id") or "").strip()
        if not r_pid or r_pid == pid:
            out.append(r)
    return out


def _sort_key(record: Dict[str, Any]) -> datetime:
    return _parse_ts(contract_invalid_effective_timestamp(record)) or _MIN_AWARE_DATETIME


def _latest_receipt_per_dispatch(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Latest-by-effective-timestamp receipt for each dispatch_id.

    Records with no dispatch_id are dropped — not attributable to any single
    open/closed decision.
    """
    by_dispatch: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        did = str(rec.get("dispatch_id") or "").strip()
        if not did:
            continue
        by_dispatch.setdefault(did, []).append(rec)
    return {did: sorted(recs, key=_sort_key)[-1] for did, recs in by_dispatch.items()}


def build_contract_invalid_summary(
    receipts_path: Path,
    *,
    now: Optional[datetime] = None,
    window_hours: int = 24,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Counters for SessionStart visibility (OI-1638 gevolg 1).

    Counts every contract_invalid RECORD in the ledger — total, and the
    subset within ``window_hours`` of ``now`` — broken down per provider.
    Deliberately not deduplicated to one-per-dispatch: a dispatch that failed
    the contract twice is two distinct governed-failure events, and the
    write side (envelope_govern.py) appends one receipt per attempt. Use
    ``collect_contract_invalid_open`` for the per-dispatch open/closed view.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    records = _filter_project(_read_receipts(receipts_path), project_id)

    total = 0
    last_window = 0
    by_provider: Dict[str, int] = {}
    by_provider_window: Dict[str, int] = {}
    for rec in records:
        if not _is_contract_invalid(rec):
            continue
        provider = str(rec.get("provider") or "unknown").strip().lower() or "unknown"
        total += 1
        by_provider[provider] = by_provider.get(provider, 0) + 1
        ts = _parse_ts(contract_invalid_effective_timestamp(rec))
        if ts is not None and ts >= cutoff:
            last_window += 1
            by_provider_window[provider] = by_provider_window.get(provider, 0) + 1

    return {
        "total": total,
        "last_24h": last_window,
        "window_hours": window_hours,
        "by_provider": by_provider,
        "by_provider_last_24h": by_provider_window,
    }


def collect_contract_invalid_open(
    receipts_path: Path,
    *,
    project_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Open contract_invalid dispatches — one entry per dispatch, not per
    receipt (OI-1638 gevolg 2, "een lijst, geen vloed").

    A dispatch is OPEN when the LATEST receipt on record for its dispatch_id
    is itself a contract_invalid receipt — i.e. no later attempt superseded
    it with a governed success. A dispatch whose latest receipt is a
    governed success is resolved and excluded entirely (not carried forward
    as a closed row — the caller wants the open backlog, not a growing
    history).
    """
    records = _filter_project(_read_receipts(receipts_path), project_id)
    latest = _latest_receipt_per_dispatch(records)

    open_items: List[Dict[str, Any]] = []
    for dispatch_id, rec in latest.items():
        if not _is_contract_invalid(rec):
            continue
        open_items.append({
            "dispatch_id": dispatch_id,
            "provider": str(rec.get("provider") or "unknown"),
            "timestamp": contract_invalid_effective_timestamp(rec),
            "report_path": rec.get("report_path"),
            "resolved": False,
        })
    open_items.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    return open_items


def write_contract_invalid_open_ledger(
    receipts_path: Path,
    output_path: Path,
    *,
    project_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Write the single-file open-items ledger atomically.

    Atomic write via ``atomic_io.atomic_write_json`` — this file is read at
    SessionStart while a dispatch may concurrently be appending to the
    ledger it is derived from; a bare ``open(path, 'w')`` could hand a
    reader a truncated/partial file mid-write, and a scratch name derived
    only from ``output_path`` would be shared by every concurrent writer of
    this same destination (OI-1486) — ``atomic_write_json`` uses a
    per-writer ``mkstemp`` name instead.
    """
    open_items = collect_contract_invalid_open(receipts_path, project_id=project_id)
    payload = {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "open_count": len(open_items),
        "items": open_items,
    }
    atomic_write_json(output_path, payload, indent=2)
    return payload


def evaluate_deliverable_acceptance(
    dispatch_id: str,
    receipts_path: Path,
    *,
    project_id: Optional[str] = None,
    strict: bool = True,
) -> DeliverableAcceptance:
    """The full judgment behind ``is_deliverable_acceptable``: the same
    boolean and the same prose, plus the machine-readable ``code`` that says
    WHICH of the six outcomes produced it (see the ``CODE_*`` constants).

    Callers that only display the answer keep using
    ``is_deliverable_acceptable``. A caller that DECIDES on the answer —
    today the merge door, whose ``--override-contract-invalid`` may bypass
    ``CODE_CONTRACT_INVALID`` and nothing else (OI-1666) — must use this one:
    the three refusals are indistinguishable in the boolean and separable in
    the prose only by matching Dutch text, which is not a contract.
    """
    did = (dispatch_id or "").strip()
    if not did:
        return DeliverableAcceptance(False, CODE_EMPTY_DISPATCH_ID, "empty dispatch_id")

    try:
        records = _read_receipts_strict(receipts_path) if strict else _read_receipts(receipts_path)
    except ReceiptsUnreadableError as exc:
        return DeliverableAcceptance(
            False, CODE_LEDGER_UNREADABLE, f"grootboek onleesbaar: {exc}",
        )

    records = _filter_project(records, project_id)
    outcomes = [
        r for r in records
        if str(r.get("dispatch_id") or "").strip() == did and _is_outcome_receipt(r)
    ]
    if not outcomes:
        return DeliverableAcceptance(
            True,
            CODE_NO_OUTCOME_RECEIPT,
            f"geen uitkomst-receipt op record voor {did} "
            f"(afwezigheid is geen weigering)",
        )

    decided = [r for r in outcomes if _is_decided_outcome(r)]
    if not decided:
        seen = sorted({
            str(r.get("status") or "").strip().lower() or "<leeg>" for r in outcomes
        })
        return DeliverableAcceptance(
            True,
            CODE_NO_DECIDED_OUTCOME,
            f"geen besliste uitkomst voor {did}: {len(outcomes)} uitkomst-receipt(s), "
            f"alle onbeslist (status: {', '.join(seen)}) "
            f"(afwezigheid is geen weigering)",
        )

    latest = sorted(decided, key=_sort_key)[-1]
    if _is_contract_invalid(latest):
        event_type = str(latest.get("event_type") or latest.get("event") or "")
        return DeliverableAcceptance(
            False,
            CODE_CONTRACT_INVALID,
            f"latest decided outcome receipt for {did} is contract_invalid "
            f"(event_type={event_type!r}, report_path={latest.get('report_path')!r})",
        )
    return DeliverableAcceptance(
        True, CODE_NOT_CONTRACT_INVALID, "latest decided outcome receipt is not contract_invalid",
    )


def is_deliverable_acceptable(
    dispatch_id: str,
    receipts_path: Path,
    *,
    project_id: Optional[str] = None,
    strict: bool = True,
) -> Tuple[bool, str]:
    """False while the latest DECIDED DELIVERABLE-OUTCOME receipt on record
    for ``dispatch_id`` is contract_invalid (OI-1638 gevolg 3) — a dispatch
    must not be silently closed on the strength of a report that never
    satisfied the report-body contract, even if an earlier attempt for the
    same dispatch_id succeeded.

    "Latest DECIDED OUTCOME receipt", not "latest receipt" — two narrowings,
    both measured 07-09 on the live ledger over the 8 dispatch-ids that
    carried a contract_invalid receipt before their merge:

      - Every receipt plane shares the dispatch_id, so the plain latest is
        usually a gate-plane record: all 8 had a ``review_gate_request``
        written on the same dispatch_id in between, and the unfiltered
        version said GO on every one of them. Only
        ``DELIVERABLE_OUTCOME_EVENT_TYPES`` is judged.
      - An outcome receipt that decides nothing may not overturn a governed
        refusal by being later: 2 of those 8 still passed on a
        ``task_complete``/``unknown`` written a minute after the
        contract_invalid. Only ``DECIDED_OUTCOME_STATUSES`` is judged.

    See both constants for the counts behind them. An undecided outcome
    receipt is skipped at selection time, exactly like a gate-plane one — not
    treated as a refusal itself, which would turn the 3897 ``unknown``
    records in that ledger into a merge blockade of their own.

    Two distinct fail-open absences, kept distinguishable in the reason
    string, per the fleet's own precedent (OI-1624, "a gate outage is
    absence, never a rejection"): no outcome receipt at all ("geen
    uitkomst-receipt"), and outcome receipts that are all undecided ("geen
    besliste uitkomst"). Neither is this function's concern; callers needing
    "did this dispatch even run" own that separate check themselves.

    ``strict`` (default True, this is a GATE function): an existing but
    unreadable ledger returns ``(False, "grootboek onleesbaar: ...")`` rather
    than degrading to an empty read that would look like that same
    fail-open absence. ``strict=False`` restores the advisory soft read for a
    caller that only wants a hint.

    The three refusals above are three DIFFERENT things and this signature
    cannot tell them apart — a caller that acts on the difference (the merge
    door's ``--override-contract-invalid``, which may bypass the
    contract_invalid one and neither of the other two) must call
    ``evaluate_deliverable_acceptance`` and read its ``code``. This function
    is the unchanged display form: same boolean, same prose, one judgment,
    computed in exactly one place.
    """
    result = evaluate_deliverable_acceptance(
        dispatch_id, receipts_path, project_id=project_id, strict=strict,
    )
    return result.acceptable, result.reason


__all__ = [
    "ACCEPTANCE_CODES",
    "CODE_CONTRACT_INVALID",
    "CODE_EMPTY_DISPATCH_ID",
    "CODE_LEDGER_UNREADABLE",
    "CODE_NOT_CONTRACT_INVALID",
    "CODE_NO_DECIDED_OUTCOME",
    "CODE_NO_OUTCOME_RECEIPT",
    "CONTRACT_INVALID_STATUS",
    "CONTRACT_INVALID_EVENT_TYPE",
    "DECIDED_OUTCOME_STATUSES",
    "DELIVERABLE_OUTCOME_EVENT_TYPES",
    "OPEN_LEDGER_FILENAME",
    "REFUSING_ACCEPTANCE_CODES",
    "DeliverableAcceptance",
    "ReceiptsUnreadableError",
    "build_contract_invalid_summary",
    "collect_contract_invalid_open",
    "evaluate_deliverable_acceptance",
    "write_contract_invalid_open_ledger",
    "is_deliverable_acceptable",
]
