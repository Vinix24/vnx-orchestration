"""Canonical gate result status interpretation (CFX-3).

Single source of truth for "is this gate result a pass?" across closure
verification, postmerge audit summaries, and any other consumer of files
under ``${VNX_STATE_DIR}/review_gates/results/`` (resolved via
``scripts/lib/vnx_paths``).

Schema drift fixed here:
- writers populate ``status`` with values from one canonical set
- readers call :func:`is_pass` instead of comparing fields ad hoc
- legacy ``verdict`` field is honored as fallback when ``status`` is null
"""
from __future__ import annotations

import uuid
import warnings
from typing import Any, Dict, Tuple

import gate_depth

# The two run-identity fields a gate result record carries (K3-2a).
#
# ``evidence_source`` names HOW the record came to exist (``live``,
# ``reprocessed``, ``reanchored``); it is not an identity and two runs of the
# same seat share it.
# ``execution_id`` names the ONE run the record describes: one id per seat run,
# minted before the run's first model call (:func:`new_execution_id`), carried
# unchanged onto every record of that run. It is the identity the cost ledger
# books on (K3), and it exists BEFORE the result slot does — deriving it from
# anything the slot hands back (an ``attempt_seq``, a timestamp, the PR, the
# head or the report) is exactly the duplicate-booking defect OI-2444 measured:
# the same run committed twice got two numbers, and two runs with a refused
# write got none.
EVIDENCE_SOURCE_FIELD = "evidence_source"
# The one ``evidence_source`` value that says the record was REBUILT from a
# report an earlier run wrote (``--reprocess``): no model was called for it, so
# it books no cost line (K3-2b). The writer is ``governed_lane._build_record``;
# the reader here is ``gate_outcome``.
EVIDENCE_SOURCE_REPROCESSED = "reprocessed"
EXECUTION_ID_FIELD = "execution_id"

# ``uuid4().hex``: 32 characters, lower-case hexadecimal, nothing else.
_EXECUTION_ID_LENGTH = 32
_EXECUTION_ID_CHARS = frozenset("0123456789abcdef")


def new_execution_id() -> str:
    """Mint the identity of ONE review-gate seat run: 32 lower-case hex characters.

    The only place an execution id is minted (K3-2a). Called once per run,
    before the run's first model call — in ``gate_runner.GateRunner`` when the
    request turns ``executing``, and in ``governed_lane.run_gate_head`` before
    the dispatcher is built. A takeover run is a run of its own and gets its
    own id. Never derived from the result slot, ``attempt_seq``, a timestamp,
    the PR, the head or the report: an identity that only exists after the
    slot write cannot tell a re-commit of one run from a second run
    (OI-2444).
    """
    return uuid.uuid4().hex


def is_execution_id(value: object) -> bool:
    """True when ``value`` is a minted execution id (32 lower-case hex characters).

    Read-side evidence: a missing, empty, non-string or differently shaped
    value is NO id, never an id to match on. A reader that books or
    de-duplicates on identity (K3-2b) must ask this before it uses a field it
    read from a record or a ledger line.
    """
    return (
        isinstance(value, str)
        and len(value) == _EXECUTION_ID_LENGTH
        and all(char in _EXECUTION_ID_CHARS for char in value)
    )

PASS_STATES = frozenset({"approve", "completed", "pass", "passed"})
FAIL_STATES = frozenset({"failed", "errored", "fail", "blocked"})
INCOMPLETE_STATES = frozenset({"pending", "running", "queued", "requested"})
# OI-1142: the provider/tool could not produce a verdict at all (quota-403, 429,
# auth failure, timeout, empty output). Absence of evidence: never a pass, never
# a fail, and not terminal — a retry against a healthy provider can still decide.
UNAVAILABLE_STATES = frozenset({"unavailable"})
# OI-1851: a clean run on a truncated diff it did not demonstrably read the
# rest of. Not a pass (the unseen part is unreviewed), not a fail (nothing was
# rejected), not an outage (a rerun cuts the same diff the same way). Terminal:
# the attempt concluded.
PARTIAL_REVIEW_STATES = frozenset({"partial_review"})

ALL_KNOWN_STATES = (
    PASS_STATES | FAIL_STATES | INCOMPLETE_STATES | UNAVAILABLE_STATES
    | PARTIAL_REVIEW_STATES | frozenset({"not_executable"})
)

# The statuses a REQUEST record ends in — the attempt it describes is over.
# ONE definition for the two sides that used to disagree (GE-4c, kimi advisory
# (c)): ``gate_outcome.REQUEST_STATUS_OF`` maps a record's outcome into this set
# and ``gate_executor.execute_gate`` short-circuits on it. ``unavailable`` was
# in the mapping and not in the executor's list, so a request ended there was
# re-run. A retry after any of these is a NEW request, never a re-run.
REQUEST_TERMINAL_STATUSES = frozenset({"completed", "failed", "not_executable", "unavailable"})


def review_coverage_gap(result: Dict[str, Any]) -> str:
    """:func:`gate_depth.coverage_gap` on the record's own ``execution_depth``
    (OI-1851); ``""`` for a record without one."""
    return gate_depth.coverage_gap(gate_depth.from_dict(result.get("execution_depth")))


def _coerce_status(result: Dict[str, Any]) -> Tuple[str, bool]:
    """Return (status, used_legacy_verdict_fallback).

    Uses ``status`` when present and non-empty. Falls back to legacy
    ``verdict`` for old files written before CFX-3 — this is graceful
    migration, not a permanent contract.  Also handles the
    ``claude_github_optional`` format where terminal outcome is recorded
    as ``state="completed"`` + ``result_status="pass"|"fail"`` rather than
    top-level ``status``/``verdict``.
    """
    status = result.get("status")
    if isinstance(status, str) and status:
        return status.lower(), False
    verdict = result.get("verdict")
    if isinstance(verdict, str) and verdict:
        warnings.warn(
            "gate_status: result file uses legacy 'verdict' field; "
            "writers should populate 'status' (CFX-3 migration).",
            DeprecationWarning,
            stacklevel=3,
        )
        return verdict.lower(), True
    if (result.get("state") or "").lower() == "completed":
        rs = (result.get("result_status") or "").lower()
        if rs:
            return rs, False
    return "", False


def is_pass(result: Dict[str, Any]) -> Tuple[bool, str]:
    """Return ``(passed, reason)`` for a gate result dict.

    ``reason`` always explains the decision so callers can surface it.
    Pass requires: canonical pass status AND zero blocking findings AND
    ``blocking_count`` is zero or absent.
    """
    status, _legacy = _coerce_status(result)
    blocking_findings = result.get("blocking_findings") or []
    blocking_len = len(blocking_findings) if isinstance(blocking_findings, list) else 0
    blocking_count = result.get("blocking_count")
    if not isinstance(blocking_count, int):
        blocking_count = None

    if status in PARTIAL_REVIEW_STATES:
        gap = review_coverage_gap(result) or (result.get("reason_detail") or "")
        return False, f"partial_review: {gap or 'the gate saw only part of the diff'}"
    if status in PASS_STATES and blocking_len == 0 and blocking_count in (0, None):
        gap = review_coverage_gap(result)
        if gap:
            return False, f"partial_review: {gap}"
        return True, "passed"
    if status in FAIL_STATES:
        return False, f"status: {status}"
    if blocking_len > 0:
        return False, f"{blocking_len} blocking finding(s)"
    if blocking_count is not None and blocking_count > 0:
        return False, f"blocking_count: {blocking_count}"
    if status in INCOMPLETE_STATES:
        return False, f"incomplete: {status}"
    if status in UNAVAILABLE_STATES:
        return False, "unavailable: provider outage — no verdict evidence (not a review fail)"
    if status == "not_executable":
        return False, "status: not_executable"
    if not status:
        return False, "no status or verdict field"
    return False, f"unknown status: {status}"


def is_terminal(result: Dict[str, Any]) -> bool:
    """True when the result represents a decided pass/fail (not in-flight).

    Used by closure verifier to decide whether to enforce report_path on
    a result (pass/fail must carry evidence; in-flight states must not).
    ``not_executable`` is treated as terminal because the gate has been
    finally classified even though no execution happened. ``unavailable``
    (OI-1142) is deliberately NOT terminal: the provider was down, no verdict
    exists, and a rerun can still decide — closure must stay blocked on
    "incomplete evidence", not read the outage as a decided outcome.
    """
    status, _ = _coerce_status(result)
    return (
        status in PASS_STATES
        or status in FAIL_STATES
        or status in PARTIAL_REVIEW_STATES
        or status == "not_executable"
    )


def canonical_status(result: Dict[str, Any]) -> str:
    """Return the canonical status string for a result, "" if unknown.

    Honors legacy ``verdict`` fallback. Always lowercased.
    """
    status, _ = _coerce_status(result)
    return status


# OI-1435: sentinel placeholders a non-normalising writer can slip into a
# string field instead of leaving it truly absent/empty. A bare non-empty
# check lets these straight through; they must count as "no evidence" too.
_EVIDENCE_SENTINEL_VALUES = frozenset({"unknown", "none", "null"})


def _is_populated_evidence_field(value: Any) -> bool:
    """True when ``value`` is a real, populated evidence-field string.

    Collapses three distinct non-values to False: the field is absent or
    the wrong type, the field is present but blank/whitespace-only, and the
    field carries a placeholder sentinel (``"unknown"``/``"none"``/``"null"``
    as literal text) rather than a real value.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    return stripped.lower() not in _EVIDENCE_SENTINEL_VALUES


def has_evidence_fields(result: Dict[str, Any]) -> bool:
    """True when ``contract_hash`` and ``report_path`` are both real, populated
    values (:func:`_is_populated_evidence_field`). The evidence-field bar on its
    own, without any statement about the verdict or the coverage of the review."""
    return _is_populated_evidence_field(result.get("contract_hash")) and _is_populated_evidence_field(
        result.get("report_path")
    )


def decided_verdict(result: Dict[str, Any]) -> str:
    """``"pass"``, ``"fail"`` or ``""`` for a gate result (OI-1943).

    The one place that answers "did this gate decide, and how", so no reader
    re-derives a fail from the status string. Review gates book a rejection as
    ``completed`` plus blocking evidence, not as a fail status.

    - ``"fail"``: the canonical status is in :data:`FAIL_STATES`, or it is in
      :data:`PASS_STATES` and the record carries blocking evidence (a non-empty
      ``blocking_findings`` list or an int ``blocking_count`` above zero). A
      coverage gap does not void a rejection: a rejection of the part the gate
      saw still stands (``gate_artifacts``).
    - ``"pass"``: :func:`is_pass`.
    - ``""``: everything else (``partial_review``, ``not_executable``,
      ``unavailable``, in-flight, unknown, and a pass status with a coverage gap
      and nothing blocking).
    """
    status, _legacy = _coerce_status(result)
    if status in FAIL_STATES:
        return "fail"
    if status in PASS_STATES:
        blocking_findings = result.get("blocking_findings")
        blocking_count = result.get("blocking_count")
        if isinstance(blocking_findings, list) and blocking_findings:
            return "fail"
        if isinstance(blocking_count, int) and not isinstance(blocking_count, bool) and blocking_count > 0:
            return "fail"
    if is_pass(result)[0]:
        return "pass"
    return ""


def has_complete_evidence(result: Dict[str, Any]) -> bool:
    """True when a gate result is terminal AND carries a complete evidence
    trail (OI-1178, hardened OI-1435).

    Requires ALL of: (1) :func:`is_terminal` — a decided pass/fail/
    not_executable outcome, never ``unavailable`` or an in-flight status;
    (2) ``contract_hash`` is a real, populated value; (3) ``report_path`` is
    a real, populated value. "Real, populated" (2 and 3) is
    :func:`_is_populated_evidence_field` — absent, blank, and
    sentinel-placeholder are three different non-values, all False here.

    OI-1435: before this fix, an ``unavailable`` record carrying non-empty
    ``contract_hash``/``report_path`` (e.g. stale values echoed forward from
    a prior attempt) read as complete evidence — True on a record with no
    decided verdict. That separation used to depend entirely on every
    caller remembering to call :func:`is_terminal` before this function; the
    terminality check now lives INSIDE the function so the invariant holds
    regardless of call order — a caller that already checks ``is_terminal``
    first is now redundant, not wrong.

    An infra failure booked by ``gate_recorder.record_failure`` carries
    neither: ``report_path`` is always "" and ``contract_hash`` echoes
    whatever the never-executed request carried, typically "". Requiring
    BOTH non-empty (on top of terminality) separates "a gate ran and
    decided" from "the gate never ran" — the latter must never be summed up
    as a PASS.

    OI-1851: nor is a review of part of the diff, whatever status the writer
    stamped (:func:`review_coverage_gap`).
    """
    if not is_terminal(result):
        return False
    if canonical_status(result) in PARTIAL_REVIEW_STATES or review_coverage_gap(result):
        return False
    return has_evidence_fields(result)


def has_producer_identity(result: Dict[str, Any]) -> bool:
    """True when a gate result carries a producer identity (OI-1093).

    Requires a non-empty ``dispatch_id``. That field is the one that ties a
    result back to a real governed dispatch in the audit trail — every
    process that writes through the fleet's single-entry dispatch door
    produces one. ``provider``/``model`` alone are not sufficient: they are
    free-text values a hand-authored JSON can carry just as easily as a real
    writer, with no corroborating trail to check them against.

    This is deliberately narrower than "is this a valid gate result" — a
    record can be well-formed (status, contract_hash, report_path all
    present) and still have no producer identity, e.g. a result written by
    hand rather than by a governed gate run. Callers decide what to do with
    that distinction; this function only answers the identity question.
    """
    dispatch_id = result.get("dispatch_id")
    return isinstance(dispatch_id, str) and bool(dispatch_id.strip())


def is_test_run_record(result: Dict[str, Any]) -> bool:
    """True when a gate result/request is an offline test run, not production evidence.

    Offline gate runs (e.g. ``kimi_gate --diff-file`` with a synthetic pr_id)
    are marked ``test_run: true`` by the writer. Such records must never count
    as real evidence — neither for closure nor for producer freshness. Boolean
    and string forms are both recognised so a non-normalising writer cannot
    slip a truthy marker past the check.
    """
    value = result.get("test_run")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return False
