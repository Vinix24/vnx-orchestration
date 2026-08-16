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

import warnings
from typing import Any, Dict, Tuple

PASS_STATES = frozenset({"approve", "completed", "pass", "passed"})
FAIL_STATES = frozenset({"failed", "errored", "fail", "blocked"})
INCOMPLETE_STATES = frozenset({"pending", "running", "queued", "requested"})
# OI-1142: the provider/tool could not produce a verdict at all (quota-403, 429,
# auth failure, timeout, empty output). Absence of evidence: never a pass, never
# a fail, and not terminal — a retry against a healthy provider can still decide.
UNAVAILABLE_STATES = frozenset({"unavailable"})

ALL_KNOWN_STATES = (
    PASS_STATES | FAIL_STATES | INCOMPLETE_STATES | UNAVAILABLE_STATES | frozenset({"not_executable"})
)


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

    if status in PASS_STATES and blocking_len == 0 and blocking_count in (0, None):
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
    return status in PASS_STATES or status in FAIL_STATES or status == "not_executable"


def canonical_status(result: Dict[str, Any]) -> str:
    """Return the canonical status string for a result, "" if unknown.

    Honors legacy ``verdict`` fallback. Always lowercased.
    """
    status, _ = _coerce_status(result)
    return status


def has_complete_evidence(result: Dict[str, Any]) -> bool:
    """True when a gate result carries a complete evidence trail (OI-1178).

    A gate that actually ran and produced a verdict materializes BOTH a
    non-empty ``contract_hash`` (the hashed contract it judged) and a
    non-empty ``report_path`` (the on-disk report of its findings). An
    infra failure booked by ``gate_recorder.record_failure`` carries
    neither: ``report_path`` is always "" and ``contract_hash`` echoes
    whatever the never-executed request carried, typically "". Requiring
    BOTH non-empty separates "a gate ran and decided" from "the gate never
    ran" — the latter must never be summed up as a PASS.
    """
    contract_hash = result.get("contract_hash")
    report_path = result.get("report_path")
    return (
        isinstance(contract_hash, str) and bool(contract_hash.strip())
        and isinstance(report_path, str) and bool(report_path.strip())
    )


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
