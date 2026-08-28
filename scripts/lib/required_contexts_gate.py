#!/usr/bin/env python3
"""required_contexts_gate.py — the merge-gate verdict for required CI contexts.

``ci_contexts`` answers a factual question: for each context branch protection
requires, does it exist on this commit, and if not, can it still appear?
This module answers the gate's question: does that add up to permission to
merge? The two are kept apart deliberately — the classifier has no opinion
about merges, and the gate has no opinion about GitHub's job graph.

It lives beside ``pre_merge_gate.py`` rather than inside it because that file
is already 1522 lines on main, past the 1200-line hard ceiling
``quality_advisory`` enforces. Growing it further to add a check would trade
one governance finding for another.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path
from typing import Any, Dict

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in _sys.path:
    _sys.path.insert(0, str(_LIB_DIR))

import ci_contexts  # noqa: E402

#: Mirrors pre_merge_gate.SKIPPED_UNVERIFIED. Defined here rather than imported
#: to keep the dependency one-way: pre_merge_gate imports this module, so an
#: import back would be a cycle. The value is asserted equal in the tests.
SKIPPED_UNVERIFIED = "SKIPPED_UNVERIFIED"

CHECK_NAME = "required_contexts"


def check_required_contexts(
    project_root: Path,
    *,
    head_sha: str,
    protected_branch: str = "main",
    timeout: int = 20,
) -> Dict[str, Any]:
    """Check that every REQUIRED branch-protection context exists on the PR head.

    ``pre_merge_gate.check_ci_workflow`` answers "did the configured CI
    workflow run and succeed on this commit". That is a different question
    from "is every check branch protection insists on actually present", and
    #1691 is the case where the two answers disagreed: nine visible passes,
    zero fails, and five of the fourteen required contexts never created at
    all during an Actions outage. Counting what is present cannot see that.

    The naive form of this check is worse than the gap it closes. #1701
    measured that: twelve green, Profile B and Profile C absent because both
    declare ``needs: profile-a`` and profile-a had not finished. A guard that
    reads "absent" as "never created" blocks every PR in the first minutes of
    its own CI. The verdict therefore keeps four outcomes apart:

      - every context passed                      -> GO
      - a context is missing for good, or failed  -> HOLD, named per context
      - a context is still coming                 -> HOLD, reported as in
        flight, so the reader knows the fix is time and not a re-run
      - anything unmeasurable                     -> SKIPPED_UNVERIFIED

    SKIPPED_UNVERIFIED, never GO, on an unreadable branch-protection list or
    an unreachable gh: ``run_gate_checks`` treats it as blocking, and a merge
    gate that could not look must not grant permission because it failed to
    look (the OI-1140 contract, applied to this second question).
    """
    try:
        states = ci_contexts.evaluate_commit(
            project_root, head_sha, branch=protected_branch, timeout=timeout,
        )
    except ci_contexts.CIContextsError as exc:
        return {
            "check": CHECK_NAME,
            "status": SKIPPED_UNVERIFIED,
            "detail": f"required contexts could not be read: {exc}",
            "contexts": [],
            "summary": None,
        }

    summary = ci_contexts.summarise(states)
    payload: Dict[str, Any] = {
        "check": CHECK_NAME,
        "contexts": [s.to_dict() for s in states],
        "summary": summary,
    }

    if not states:
        payload["status"] = SKIPPED_UNVERIFIED
        payload["detail"] = (
            f"branch protection on {protected_branch!r} lists no required contexts — "
            "nothing to verify, which is a misconfiguration rather than a pass"
        )
        return payload

    if summary["unverified"]:
        unverified = [s.context for s in states if s.state == ci_contexts.STATE_UNVERIFIED]
        payload["status"] = SKIPPED_UNVERIFIED
        payload["detail"] = (
            f"{len(unverified)} of {summary['total']} required contexts could not be "
            f"classified: {', '.join(unverified)}"
        )
        return payload

    blocking = [s for s in states if s.blocking]
    if not blocking:
        payload["status"] = "GO"
        payload["detail"] = f"all {summary['total']} required contexts passed on {head_sha[:12]}"
        return payload

    payload["status"] = "HOLD"
    payload["detail"] = _describe_blocking(blocking, summary["total"], head_sha)
    return payload


def _describe_blocking(blocking, total: int, head_sha: str) -> str:
    """One line naming what blocks, with settled and in-flight kept apart."""
    settled = [s for s in blocking if not s.transient]
    in_flight = [s for s in blocking if s.transient]
    parts = []
    if settled:
        parts.append(
            "not satisfied: " + "; ".join(f"{s.context} [{s.state}] {s.detail}" for s in settled)
        )
    if in_flight:
        parts.append("still in flight: " + ", ".join(f"{s.context} [{s.state}]" for s in in_flight))
    return (
        f"{len(blocking)} of {total} required contexts block the merge on "
        f"{head_sha[:12]} — " + " | ".join(parts)
    )
