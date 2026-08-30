#!/usr/bin/env python3
"""VNX PR merge: merge a PR and emit pr_merged receipt + dispatch register event.

T0 calls this instead of raw ``gh pr merge`` so every merge is captured in the
audit trail (t0_receipts.ndjson + dispatch_register.ndjson).  Without this,
FPY/rework-rate/history have no linkage between merged PRs and receipts.

Merge gate (OI-1216): before merging, the CLI runs a fail-closed check that the
VNX CI workflow has a run with ``conclusion=success`` for exactly the PR head
SHA (see ``_run_ci_gate`` -> ``merge_preflight_ci_check.check_ci_run_for_head``).
A run on an older head does not count, zero runs is a refusal, and any
unverifiable state is a refusal — never a silent pass. ``--override-reason``
skips the check visibly with a mandatory reason.

Review gate (20260816-gate-never-skippable): a second fail-closed check runs
after the CI gate — a passing, fully-evidenced review-gate result must exist
for this PR (see ``_run_review_gate`` ->
``closure_verifier.check_review_gate_for_merge``). A missing result, an empty
``contract_hash``/``report_path``, or a verdict contradicting its report is a
refusal. The same ``--override-reason`` valve applies with a mandatory reason.

Merge pinning (OI-1264): the merge is pinned to the exact head the gates
approved. ``_run_ci_gate`` establishes the head SHA, and ``_do_merge`` passes
it as ``gh pr merge --match-head-commit <sha>`` so ``gh`` refuses to merge any
other commit. A push to the branch after the gates ran moves the head, and the
merge is then refused with a "gates must re-run" message instead of silently
merging a commit the gates never saw.

Outcome verification + conditional --auto (OI-1399 / OI-1386): ``gh pr merge
--auto`` exits 0 both when it merges immediately AND when it only *enables*
auto-merge (the actual merge stays pending on required checks/reviews) — the
two are indistinguishable from the exit code alone. ``_do_merge`` therefore
never trusts ``gh``'s exit code as proof of a merge: after a zero exit it
re-queries the PR (``_pr_actually_merged``) and only reports success when
``state == "MERGED"``. Separately, ``--auto`` is now conditional
(``_repo_auto_merge_allowed``): it is only added when the repository actually
has "Allow auto-merge" enabled, so a repo with it disabled takes the plain
``gh pr merge`` path instead of having the merge rejected outright for
requesting a repo feature that is off.

Usage:
    python3 scripts/pr_merge.py --pr 123
    python3 scripts/pr_merge.py --pr 123 --dispatch-id 20260526-gov2-something
    python3 scripts/pr_merge.py --pr 123 --squash          # default merge strategy
    python3 scripts/pr_merge.py --pr 123 --rebase
    python3 scripts/pr_merge.py --pr 123 --merge
    python3 scripts/pr_merge.py --pr 123 --dry-run         # no merge, no write
    python3 scripts/pr_merge.py --pr 123 --override-reason 'gate flaked, re-verified'

Receipt written to t0_receipts.ndjson:
    event_type  : "pr_merged"
    pr_number   : <int>
    dispatch_id : <str, optional>
    conclusion  : "merged"
    merge_method: "squash" | "merge" | "rebase"
    pr_title    : <from gh api>
    branch      : <from gh api>

Register event written to dispatch_register.ndjson:
    event       : "pr_merged"
    pr_number   : <int>
    dispatch_id : <str, optional>
    terminal    : "T0"

BILLING SAFETY: No Anthropic SDK. No direct API calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt
from merge_preflight_ci_check import check_ci_run_for_head, _resolve_override_reason

EXIT_OK = 0
EXIT_ERROR = 1

# Matches internal PR labels: pure numeric (PR-42) and alphanumeric (PR-HYG-1, PR-TMUX-3, PR-ROUTE-1).
_PR_LABEL_RE = re.compile(r"\bPR-([A-Z0-9]+(?:-[A-Z0-9]+)*)\b", re.IGNORECASE)

# OI-1518: AppendResult.status values that count as a BINDING receipt landed.
# A grep of `AppendResult(` across the whole tree yields exactly two producers
# (lib/append_receipt_internals/idempotency.py: "appended" + "duplicate";
# lib/report_to_receipt_converter.py: "duplicate"). No third value exists, so
# this is an allowlist, not a blocklist: any other value is refused. The
# "unknown" default (a missing `append_status` key) is NOT a third value — it
# means "this code could not read the outcome", which is its own third branch:
# neither success nor a known fault, so it fails safe. Two failure shapes land
# here together: _emit_receipt raising, and _emit_receipt returning a status
# that is not recognized. Both must make the CLI exit non-zero.
_RECEIPT_OK_STATUSES = frozenset({"appended", "duplicate"})


def _extract_pr_id(subject: str) -> Optional[str]:
    """Extract internal PR-N/PR-LABEL from commit subject or PR title.

    Returns the first match as an uppercase string, e.g. "PR-HYG-1" or "PR-42".
    Returns None if no internal PR label is found.
    """
    m = _PR_LABEL_RE.search(subject)
    if m:
        return f"PR-{m.group(1).upper()}"
    return None


def _lookup_dispatch_id_by_pr_number(pr_number: int) -> str:
    """Look up dispatch_id in dispatch_register by pr_number. Best-effort, returns '' on miss."""
    try:
        from dispatch_register import read_events
        events = read_events()
        for ev in reversed(events):
            if ev.get("pr_number") == pr_number and ev.get("dispatch_id"):
                return str(ev["dispatch_id"])
    except Exception as e:
        log.warning("dispatch lookup failed: %s", e)
    return ""


def _gh(args: list[str], *, check: bool = False, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a gh command and return the CompletedProcess."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _query_pr(pr_number: int) -> Optional[Dict[str, Any]]:
    """Return PR metadata from GitHub, or None on failure."""
    result = _gh([
        "pr", "view", str(pr_number),
        "--json", "number,title,state,headRefName,baseRefName,headRefOid,mergedAt,mergeCommit",
    ])
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _run_ci_gate(
    pr_number: int,
    *,
    override_reason: Optional[str] = None,
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Fail-closed merge gate: VNX CI must have conclusion=success for the PR head.

    Resolves the PR head (sha + branch) via ``gh pr view`` and delegates the
    workflow-conclusion check to
    ``merge_preflight_ci_check.check_ci_run_for_head``. Returns ``(gate,
    pr_data)``; the caller merges only when ``gate["verdict"] == "GO"``.

    A failed/empty PR query (no head sha or branch) is a NO-GO — the GitHub API
    not answering is a refusal with a message, never a silent pass.
    """
    pr_data = _query_pr(pr_number)
    head_sha = (pr_data or {}).get("headRefOid") or ""
    branch = (pr_data or {}).get("headRefName") or ""
    if not head_sha or not branch:
        return {
            "verdict": "NO-GO",
            "message": (
                f"PR-head (sha/branch) kon niet worden bepaald voor #{pr_number}: "
                "deze merge is niet toetsbaar"
            ),
            "overridden": False,
            "override_reason": None,
        }, pr_data
    gate = check_ci_run_for_head(
        SCRIPT_DIR.parent,
        branch=branch,
        head_sha=head_sha,
        override_reason=override_reason,
    )
    return gate, pr_data


def _norm_pr_id(pr_id: str) -> str:
    """Deprecated alias for ``gate_obligations.normalise_pr_id``.

    Kept as a name so existing imports and tests keep resolving; the logic
    lives in gate_obligations, which the readiness report reads from too.
    """
    from gate_obligations import normalise_pr_id

    return normalise_pr_id(pr_id)


def _resolve_declared_gate(pr_number: int, *, state_dir: Path) -> str:
    """Resolve a PR's declared review gate from its door obligation.

    Delegates the join to ``gate_obligations.declared_gates_for_pr`` and keeps
    this function's own contract unchanged: the LAST declared gate wins, and
    an unreadable obligation store degrades to "" (a refusal at the merge
    gate) rather than raising into the merge path.
    """
    try:
        from gate_obligations import declared_gates_for_pr

        matches = declared_gates_for_pr(state_dir, pr_number)
        if matches:
            return matches[-1]
    except (ValueError, OSError) as exc:
        log.warning("review-gate obligation lookup failed: %s", exc)
    return ""


def _run_review_gate(
    pr_number: int,
    *,
    override_reason: Optional[str] = None,
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Fail-closed review-gate check: a passing, evidenced review-gate result
    must exist for this PR before merge.

    Resolves the PR branch via ``gh pr view``, resolves the declared gate from
    the door's obligation record (joined on the GitHub PR number), then
    delegates result validation to
    ``closure_verifier.check_review_gate_for_merge`` — the SAME truth the
    closure verifier uses. Returns ``(gate, pr_data)``; the caller merges only
    when ``gate["verdict"] == "GO"``.

    The result key is the bare PR number string (``str(pr_number)``): the
    governed obligation runner records gate results under that key regardless
    of any PR-N label the spec declared. A failed PR query, a missing
    obligation, or a missing/contradictory result is a NO-GO — an unverifiable
    state is a refusal, never a silent pass. ``override_reason`` is the escape
    hatch with the same semantics as the CI gate: non-empty skips the check
    visibly, empty is refused.
    """
    pr_data = _query_pr(pr_number)
    if pr_data is None:
        return {
            "verdict": "NO-GO",
            "message": (
                f"PR-data kon niet worden opgevraagd voor #{pr_number}: "
                "review-gate-check niet toetsbaar"
            ),
            "overridden": False,
            "override_reason": None,
            "gate": None,
        }, pr_data
    branch = pr_data.get("headRefName") or ""
    head_sha = pr_data.get("headRefOid") or ""
    if not head_sha or not branch:
        # OI-1318: the sibling CI gate refuses here and this one did not. It
        # carried on with empty strings into check_review_gate_for_merge, whose
        # matcher then read "" as "no constraint" and accepted any result for
        # the PR — so the one path that could not establish which commit it was
        # merging was also the path that stopped asking. Same refusal, same
        # words, as _run_ci_gate.
        return {
            "verdict": "NO-GO",
            "message": (
                f"PR-head (sha/branch) kon niet worden bepaald voor #{pr_number}: "
                "review-gate-check niet toetsbaar"
            ),
            "overridden": False,
            "override_reason": None,
            "gate": None,
        }, pr_data

    # ── Escape hatch (same resolution + semantics as the CI gate) ─────────
    reason = _resolve_override_reason(override_reason)
    if reason is not None:
        if not reason:
            return {
                "verdict": "NO-GO",
                "message": (
                    "override zonder reden geweigerd: een override vereist een "
                    "niet-lege reden (geen stille bypass)"
                ),
                "overridden": True,
                "override_reason": reason,
                "gate": None,
            }, pr_data
        return {
            "verdict": "GO",
            "message": f"OVERRIDE: review-gate-check overgeslagen voor merge ({reason})",
            "overridden": True,
            "override_reason": reason,
            "gate": None,
        }, pr_data

    # ── Declared gate from the door's obligation ──────────────────────────
    paths = ensure_env()
    state_dir = Path(paths["VNX_STATE_DIR"])
    gate_name = _resolve_declared_gate(pr_number, state_dir=state_dir)
    if not gate_name:
        return {
            "verdict": "NO-GO",
            "message": (
                f"geen review-gate-verplichting gevonden voor PR #{pr_number}: "
                "weiger merge zonder gate-verdict"
            ),
            "overridden": False,
            "override_reason": None,
            "gate": None,
        }, pr_data

    from closure_verifier import check_review_gate_for_merge

    results_dir = state_dir / "review_gates" / "results"
    gate = check_review_gate_for_merge(
        str(pr_number),
        gate_name,
        results_dir,
        branch=branch,
        head_sha=head_sha,
    )
    return gate, pr_data


_HEAD_MOVED_MARKERS = (
    "head branch is not up to date",
    "head branch was modified",
    "head was modified",
    "head changed",
    "head has changed",
)


def _is_head_moved_refusal(output: str) -> bool:
    """True when a failed ``gh pr merge`` refusal signals the PR head moved.

    ``gh`` surfaces the GitHub server's refusal text verbatim and has no stable
    exit-code vocabulary for a ``--match-head-commit`` mismatch, so this matches
    the phrasings GitHub emits when the head no longer equals the pinned commit.
    A non-match falls through to the raw error, never to a silent pass.
    """
    low = (output or "").lower()
    return any(marker in low for marker in _HEAD_MOVED_MARKERS)


def _head_moved_refusal_message(pr_number: int, head_sha: str, raw: str) -> str:
    """Explain a head-moved refusal as the gates working, not a crash."""
    return (
        f"merge van PR #{pr_number} geweigerd: de head van de branch is "
        f"verschoven na de goedkeuring (goedgekeurd op {head_sha}). "
        "Er is na de CI-/review-gate naar de branch gepusht; de gates moeten "
        "opnieuw draaien op de nieuwe head voordat deze PR gemerged mag worden. "
        f"gh: {raw}"
    )


def _repo_auto_merge_allowed() -> Optional[bool]:
    """Whether the repo has "Allow auto-merge" enabled — required for ``gh --auto``.

    Neither ``gh pr view`` nor ``gh repo view --json`` expose this setting; the
    REST repo object carries it as ``allow_auto_merge``, so it is queried via
    ``gh api``. Returns ``None`` when it could not be determined (``gh``
    failure, unparseable output) — the caller must treat that the same as
    "not available", never as "available": assuming availability is exactly
    OI-1386 (``--auto`` requested on a repo where the setting is off, and
    ``gh`` rejects the merge outright for it).
    """
    result = _gh(["api", "repos/{owner}/{repo}", "--jq", ".allow_auto_merge"])
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip().lower()
    if out == "true":
        return True
    if out == "false":
        return False
    return None


def _pr_actually_merged(pr_number: int) -> tuple[bool, str]:
    """Verify a PR is really merged — never trust a ``gh pr merge`` exit code alone.

    ``gh pr merge --auto`` exits 0 both when it merges immediately AND when it
    only *enables* auto-merge (the merge itself stays pending on required
    checks/reviews that have not landed yet). Both look identical on the exit
    code; only the PR's own state distinguishes them (OI-1399). An
    unqueryable PR state is treated as "not merged" — never a silent pass.
    """
    pr_data = _query_pr(pr_number)
    if pr_data is None:
        return False, (
            f"gh pr merge meldde succes voor #{pr_number}, maar de PR-state kon niet "
            "worden opgevraagd om dat te bevestigen: behandel dit als een mislukte merge"
        )
    state = (pr_data.get("state") or "").upper()
    if state == "MERGED":
        return True, ""
    return False, (
        f"gh pr merge meldde succes voor #{pr_number}, maar de PR staat nog op "
        f"state={state or 'onbekend'} (geen MERGED): vermoedelijk staat auto-merge nog "
        "te wachten op openstaande vereisten. Dit telt niet als een voltooide merge."
    )


def _do_merge(pr_number: int, method: str, head_sha: str = "") -> tuple[bool, str]:
    """Execute gh pr merge pinned to the approved head and return (success, error).

    ``head_sha`` is the exact commit the CI and review gates approved. It is
    passed as ``--match-head-commit`` so ``gh`` refuses to merge any other
    commit: a push to the branch after the gates ran moves the head, and the
    merge must then be refused (so the gates re-run) rather than silently merged.
    A head-moved refusal is reported as that — the system working — not as a
    generic ``gh`` failure.

    ``--auto`` is added only when the repo actually supports it
    (``_repo_auto_merge_allowed``) — see module docstring (OI-1386). A ``gh``
    exit 0 is not itself treated as success: the PR state is re-queried
    (``_pr_actually_merged``) and only ``state == "MERGED"`` counts (OI-1399).
    """
    method_flag = f"--{method}"
    args = ["pr", "merge", str(pr_number), method_flag]
    if head_sha:
        args += ["--match-head-commit", head_sha]
    if _repo_auto_merge_allowed():
        args = args + ["--auto"]

    result = _gh(args)
    if result.returncode != 0:
        raw = (result.stderr or result.stdout or "gh pr merge failed").strip()
        if head_sha and _is_head_moved_refusal(raw):
            return False, _head_moved_refusal_message(pr_number, head_sha, raw)
        return False, raw
    return _pr_actually_merged(pr_number)


def _emit_receipt(
    *,
    pr_number: int,
    dispatch_id: str,
    merge_method: str,
    pr_title: str,
    branch: str,
    pr_id: str = "",
    pr_id_resolution: str = "",
    receipts_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Write pr_merged receipt to t0_receipts.ndjson with dual-scheme linkage.

    Dual-scheme: pr_number (GitHub numeric) + pr_id (internal PR-N/PR-LABEL).
    pr_id_resolution='unmatched' when no internal label could be derived.
    """
    kwargs: Dict[str, Any] = {
        "pr_number": pr_number,
        "conclusion": "merged",
        "merge_method": merge_method,
        "pr_title": pr_title,
        "branch": branch,
    }
    if pr_id:
        kwargs["pr_id"] = pr_id
    elif pr_id_resolution:
        kwargs["pr_id_resolution"] = pr_id_resolution
    if dispatch_id:
        kwargs["dispatch_id"] = dispatch_id
    return emit_governance_receipt(
        "pr_merged",
        receipt_kind="state_mutation",
        status="success",
        terminal="T0",
        source="pr_merge",
        receipts_file=receipts_file,
        **kwargs,
    )


def _emit_register_event(
    *,
    pr_number: int,
    dispatch_id: str,
    merge_method: str,
) -> bool:
    """Write pr_merged event to dispatch_register.ndjson. Best-effort, never raises."""
    try:
        from dispatch_register import append_event
        return append_event(
            "pr_merged",
            pr_number=pr_number,
            dispatch_id=dispatch_id or "",
            terminal="T0",
            extra={"merge_method": merge_method, "conclusion": "merged"},
        )
    except Exception:
        return False


def merge_pr(
    pr_number: int,
    *,
    dispatch_id: str = "",
    merge_method: str = "squash",
    dry_run: bool = False,
    receipts_file: Optional[str] = None,
    head_sha: str = "",
    pr_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge a PR and emit audit trail.

    ``head_sha`` is the exact commit the gates approved; it is threaded into
    ``gh pr merge --match-head-commit`` so the merge refuses any other commit.
    It is established once by ``_run_ci_gate`` and must not be re-fetched here
    (a second fetch would be a second source for the same identity). Empty
    (default) preserves the pre-pinning behavior for callers that never ran a
    gate.

    ``pr_data`` lets the caller pass PR metadata it already fetched (``main()``
    already has it from ``_run_ci_gate``) so this does not re-query ``gh`` a
    third time per invocation for the same PR just to read title/branch.
    ``None`` (default) preserves the previous self-fetching behavior for
    callers that never ran a gate.

    Returns a dict with keys: success, pr_number, dispatch_id, merge_method,
    pr_title, branch, receipt_status, receipt_ok, register_ok, error.

    ``success`` means "the merge happened on GitHub" — it is NOT the CLI
    verdict. A merge whose receipt could not be written has ``success=True``
    but ``receipt_ok=False``, and the CLI exits non-zero (OI-1518). Use
    ``receipt_ok`` to know whether the binding audit-trail evidence landed.
    """
    result: Dict[str, Any] = {
        "success": False,
        "pr_number": pr_number,
        "dispatch_id": dispatch_id,
        "merge_method": merge_method,
        "pr_title": "",
        "branch": "",
        "receipt_status": None,
        "receipt_ok": False,
        "register_ok": False,
        "error": "",
        "dry_run": dry_run,
        "overlaps": [],
    }

    # PR metadata for the receipt (title/branch). Reuse what the caller
    # already fetched when given; otherwise fetch it here (pre-pinning
    # behavior for callers that never ran a gate).
    if pr_data is None:
        pr_data = _query_pr(pr_number)
    if pr_data:
        result["pr_title"] = pr_data.get("title", "")
        result["branch"] = pr_data.get("headRefName", "")

    # OI-1091: warn (never block) when another OPEN dispatch branch touches the same files as
    # the branch being merged. Best-effort; a git/network failure degrades to no warning.
    if result["branch"]:
        try:
            from file_scope_overlap import warn_overlaps  # noqa: PLC0415
            result["overlaps"] = warn_overlaps(result["branch"], repo=SCRIPT_DIR.parent)
        except Exception as exc:  # noqa: BLE001 — an overlap check must never block a merge
            log.warning("file-scope overlap check failed for PR #%s: %s", pr_number, exc)

    if dry_run:
        result["success"] = True
        result["error"] = "dry_run: no merge executed"
        print(f"[dry-run] Would merge PR #{pr_number} via {merge_method}")
        if dispatch_id:
            print(f"[dry-run] dispatch_id: {dispatch_id}")
        return result

    # Execute the merge, pinned to the head the gates approved.
    ok, err = _do_merge(pr_number, merge_method, head_sha)
    if not ok:
        result["error"] = err
        print(f"ERROR: gh pr merge failed for #{pr_number}: {err}", file=sys.stderr)
        return result

    result["success"] = True

    # Derive internal PR-N label from title for dual-scheme receipt
    pr_id = _extract_pr_id(result["pr_title"] or result["branch"])
    if not dispatch_id:
        dispatch_id = _lookup_dispatch_id_by_pr_number(pr_number)
    result["dispatch_id"] = dispatch_id

    # Emit receipt to t0_receipts.ndjson. OI-1518: the receipt is BINDING — a
    # merge whose proof could not be written must NOT exit 0. Two failure
    # shapes both land here: _emit_receipt raising (handled below), and
    # _emit_receipt returning an `append_status` that is not a recognized
    # success value (including a missing key -> "unknown" default). Both set
    # receipt_ok=False so the CLI exits non-zero. The register event stays
    # best-effort and is not touched.
    try:
        receipt = _emit_receipt(
            pr_number=pr_number,
            dispatch_id=dispatch_id,
            merge_method=merge_method,
            pr_title=result["pr_title"],
            branch=result["branch"],
            pr_id=pr_id or "",
            pr_id_resolution="" if pr_id else "unmatched",
            receipts_file=receipts_file,
        )
        append_status = (receipt or {}).get("append_status", "unknown")
        result["receipt_status"] = append_status
        result["receipt_ok"] = append_status in _RECEIPT_OK_STATUSES
        if not result["receipt_ok"]:
            print(
                f"FATAL: merge happened for PR #{pr_number} but the receipt did NOT "
                f"land (append_status={append_status!r}). The merge on GitHub is "
                f"irreversible. Re-run `python3 scripts/pr_merge.py --pr {pr_number}` "
                f"after fixing the receipts file so the audit-trail evidence is captured.",
                file=sys.stderr,
            )
    except Exception as exc:
        result["receipt_status"] = f"error: {exc}"
        result["receipt_ok"] = False
        print(
            f"FATAL: merge happened for PR #{pr_number} but the receipt could NOT be "
            f"written: {exc}. The merge on GitHub is irreversible. Re-run "
            f"`python3 scripts/pr_merge.py --pr {pr_number}` after fixing the error "
            f"so the audit-trail evidence is captured.",
            file=sys.stderr,
        )

    # Emit event to dispatch_register.ndjson (best-effort)
    result["register_ok"] = _emit_register_event(
        pr_number=pr_number,
        dispatch_id=dispatch_id,
        merge_method=merge_method,
    )

    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge a PR and emit pr_merged receipt + register event",
    )
    parser.add_argument("--pr", type=int, required=True, help="GitHub PR number")
    parser.add_argument(
        "--dispatch-id", default="",
        help="Dispatch-ID to link this merge to a receipt chain",
    )
    merge_group = parser.add_mutually_exclusive_group()
    merge_group.add_argument("--squash", dest="merge_method", action="store_const", const="squash", default=None)
    merge_group.add_argument("--rebase", dest="merge_method", action="store_const", const="rebase")
    merge_group.add_argument("--merge", dest="merge_method", action="store_const", const="merge")
    parser.add_argument("--dry-run", action="store_true", help="Skip merge and receipt write")
    parser.add_argument(
        "--override-reason", default=None,
        help="Escape hatch: skip the CI gate with this required reason (empty is refused). "
             "Also read from VNX_MERGE_OVERRIDE_REASON.",
    )
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    args = parser.parse_args(argv)

    method = args.merge_method or "squash"

    # ── Merge gate: VNX CI conclusion=success for the exact PR head ──────
    gate, pr_data = _run_ci_gate(args.pr, override_reason=args.override_reason)
    if gate["verdict"] != "GO":
        if args.json:
            print(json.dumps({
                "success": False,
                "pr_number": args.pr,
                "error": gate["message"],
                "ci_gate": gate,
            }, indent=2))
        else:
            print(f"NO-GO: {gate['message']}", file=sys.stderr)
        return EXIT_ERROR
    if gate.get("overridden"):
        print(f"OVERRIDE: {gate['message']}")
    else:
        print(f"CI gate: {gate['message']}")

    # ── Review gate: a passing, evidenced review-gate result must exist ────
    review_gate, _ = _run_review_gate(args.pr, override_reason=args.override_reason)
    if review_gate["verdict"] != "GO":
        if args.json:
            print(json.dumps({
                "success": False,
                "pr_number": args.pr,
                "error": review_gate["message"],
                "review_gate": review_gate,
            }, indent=2))
        else:
            print(f"NO-GO: {review_gate['message']}", file=sys.stderr)
        return EXIT_ERROR
    if review_gate.get("overridden"):
        print(f"OVERRIDE: {review_gate['message']}")
    else:
        print(f"Review gate: {review_gate['message']}")

    # The head SHA the gates approved is established once in _run_ci_gate; the
    # merge is pinned to it (--match-head-commit) so a post-gate push is refused.
    head_sha = (pr_data or {}).get("headRefOid") or ""

    result = merge_pr(
        pr_number=args.pr,
        dispatch_id=args.dispatch_id or "",
        merge_method=method,
        dry_run=args.dry_run,
        head_sha=head_sha,
        pr_data=pr_data,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    elif args.dry_run:
        # dry-run: no merge, no receipt, exit 0 (unchanged behavior).
        print(f"OK: PR #{args.pr} (dry-run) via {method}")
    elif result["success"] and result["receipt_ok"]:
        # merge gebeurd + receipt geland -> exit 0
        print(f"OK: PR #{args.pr} merged via {method}")
        if result.get("receipt_status"):
            print(f"    receipt: {result['receipt_status']}")
        print(f"    register: {'ok' if result['register_ok'] else 'warn-not-written'}")
    elif result["success"] and not result["receipt_ok"]:
        # merge gebeurd + receipt NIET geland -> non-zero, tekst maakt
        # onmiskenbaar duidelijk dat de merge wél is doorgegaan en alleen
        # het bewijs ontbreekt (OI-1518). De FATAL-regel is al op stderr
        # gezet in merge_pr(); hier de korte samenvatting op stdout.
        print(
            f"ERROR: PR #{args.pr} WAS MERGED but the receipt did not land "
            f"(status={result.get('receipt_status')!r}). The merge is irreversible; "
            f"only the audit-trail proof is missing. See stderr.",
            file=sys.stderr,
        )
    else:
        # merge niet gebeurd -> non-zero, bestaande tekst
        print(f"ERROR: {result['error']}", file=sys.stderr)

    # CLI verdict (OI-1518): exit 0 only on dry-run, or on a merge whose
    # binding receipt actually landed. A merge without proof is non-zero.
    if args.dry_run:
        return EXIT_OK
    if result["success"] and result["receipt_ok"]:
        return EXIT_OK
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
