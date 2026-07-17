#!/usr/bin/env python3
"""Receipt v2 verdict computation — ADR-035 §3.1.

Pure function: `compute_verdict(receipt) -> dict`. Reads only the receipt
dict passed in — no I/O, no side effects, no LLM judgment. Deterministic
and unit-testable by design (ADR-035 §11 rejects an LLM-computed verdict
outright).

This is PR-1 of the ADR-035 §9 decomposition: additive dead code, not wired
into any write path yet (that is PR-3/PR-4).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# The verified union of hard-failure `status` literals across both write
# paths (dispatch_envelope.py/provider_dispatch.py for Path 1,
# report_parser.py for Path 2) — ADR-035 §3.1, r4. Note both "failed" and
# "failure" are included: they are distinct literals used by different
# call sites, not a typo of one another.
HARD_FAILURE_STATUSES = frozenset(
    {
        "failed",
        "failure",
        "error",
        "blocked",
        "timeout",
        "contract_invalid",
    }
)

# verification.method values that mean "we don't actually have evidence"
# rather than "we checked and it's clean" — ADR-035 §3.1 (evidence_complete).
INCOMPLETE_EVIDENCE_METHODS = frozenset({"unknown", "none_claimed", "pending-report"})


def _doc_only_path(path: str) -> bool:
    return path.startswith("docs/") or path.endswith(".md")


def _doc_only_paths_confirmed(receipt: Dict[str, Any]) -> bool:
    """True only when every changed path is verifiably under docs/**/*.md.

    Fail-safe on absence: a missing/null/empty `paths` list is NOT treated
    as "no paths to violate the rule" — it is treated as unproven doc-only-
    ness (ADR-035 §3.1.1, r2 HIGH-4). Absence of evidence is not evidence
    of doc-only.
    """
    provenance = receipt.get("provenance") or {}
    diff_summary = provenance.get("diff_summary") or {}
    paths = diff_summary.get("paths")
    if not paths:
        return False
    return all(_doc_only_path(p) for p in paths)


def _has_blocker_warning(warnings: List[Any]) -> bool:
    return any((w or {}).get("severity") == "blocker" for w in warnings)


def _has_oi_pending_warning(warnings: List[Any]) -> bool:
    return any((w or {}).get("destination") == "oi_pending" for w in warnings)


def _verdict(decision: str, reason: str, evidence_complete: bool) -> Dict[str, Any]:
    return {
        "decision": decision,
        "reason": reason,
        "evidence_complete": evidence_complete,
    }


def compute_verdict(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Compute `{decision, reason, evidence_complete}` for a receipt.

    Pure function over the rule table in ADR-035 §3.1/§3.1.1. Evaluation
    order (highest precedence first):

      1. reject   — hard-failure `status`, or any `severity: "blocker"` warning.
      2. doc-only — `verification.method == "n/a"`: accept only if every
                     changed path is confirmed under docs/**/*.md, else
                     investigate (fail-safe on missing/partial evidence).
      3. investigate — `verification.method == "pending-report"`, or an
                     unresolved `destination: "oi_pending"` warning, or a
                     success-claiming status with missing/incomplete test
                     evidence.
      4. accept   — success-claiming status, `tests_failed == 0` with
                     `tests_run > 0`, no blocker/oi_pending warnings.
    """
    status = receipt.get("status")
    warnings = receipt.get("warnings") or []
    verification = receipt.get("verification") or {}
    method: Optional[str] = verification.get("method")

    evidence_complete = method not in INCOMPLETE_EVIDENCE_METHODS

    if status in HARD_FAILURE_STATUSES:
        return _verdict(
            "reject",
            f"status={status!r} is a hard-failure status",
            evidence_complete,
        )

    if _has_blocker_warning(warnings):
        return _verdict(
            "reject",
            "an unresolved severity=blocker warning is present",
            evidence_complete,
        )

    if method == "n/a":
        if _doc_only_paths_confirmed(receipt):
            if _has_oi_pending_warning(warnings):
                return _verdict(
                    "investigate",
                    "doc-only change (verification.method=n/a) but an "
                    "oi_pending warning is unresolved",
                    evidence_complete,
                )
            return _verdict(
                "accept",
                "verification.method=n/a and every changed path is under "
                "docs/**/*.md",
                evidence_complete,
            )
        return _verdict(
            "investigate",
            "verification.method=n/a but provenance.diff_summary.paths is "
            "missing, empty, or contains a non-doc path",
            evidence_complete,
        )

    if method == "pending-report":
        return _verdict(
            "investigate",
            "verification.method=pending-report, no test evidence available yet",
            evidence_complete,
        )

    if _has_oi_pending_warning(warnings):
        return _verdict(
            "investigate",
            "an unresolved destination=oi_pending warning is present",
            evidence_complete,
        )

    tests_run = verification.get("tests_run")
    tests_failed = verification.get("tests_failed")

    if not tests_run or tests_run <= 0 or tests_failed != 0:
        return _verdict(
            "investigate",
            f"status={status!r} claims success but verification test "
            "evidence is absent or incomplete",
            evidence_complete,
        )

    return _verdict(
        "accept",
        f"status={status!r}, verification {tests_run - tests_failed}/{tests_run} "
        "tests passed, no blocker or oi_pending warnings",
        evidence_complete,
    )
