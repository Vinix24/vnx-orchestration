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
    """Normalize an internal PR id for obligation matching.

    "PR-879", "pr879" and "879" all normalize to "879"; "PR-HYG-1" to "HYG-1".
    The door stores the raw spec pr_id on the obligation; the merge gate joins
    that against the GitHub PR number, which may differ in case, hyphen and the
    "PR" prefix, so comparison goes through this normalization.
    """
    s = (pr_id or "").strip().upper()
    if s.startswith("PR-"):
        return s[3:]
    # Bare "PR<digits>" (e.g. "pr879") — strip the prefix only when it is
    # followed by a digit, so alphanumeric labels like "PR-HYG-1" (handled
    # above) and words that merely start with "PR" are never mangled.
    if s.startswith("PR") and len(s) > 2 and s[2].isdigit():
        return s[2:]
    return s


def _resolve_declared_gate(pr_number: int, *, state_dir: Path) -> str:
    """Resolve a PR's declared review gate from its door obligation.

    The obligation the dispatch door writes (``scripts/lib/gate_obligations.py``)
    carries the declared gate. The merge-time join key is the GitHub PR number:
    the runner stamps ``pr_number`` on the obligation it fulfils, and a numeric
    pr_id ("PR-1584" / "1584" / "pr1584") normalizes to the same number.
    Returns the gate name, or "" when no obligation declares one — the merge
    gate treats "" as a refusal, never a pass.
    """
    try:
        from gate_obligations import NO_GATE_KEY, iter_obligations

        num = str(pr_number)
        num_forms = {_norm_pr_id(num), _norm_pr_id(f"PR-{num}")}
        matches = []
        for _path, record in iter_obligations(state_dir):
            rec_num = record.get("pr_number")
            matched = rec_num is not None and str(rec_num) == num
            if not matched:
                matched = _norm_pr_id(str(record.get("pr_id") or "")) in num_forms
            if not matched:
                continue
            gate = (record.get("gate") or "").strip()
            if gate and gate != NO_GATE_KEY:
                matches.append(gate)
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


def _do_merge(pr_number: int, method: str) -> tuple[bool, str]:
    """Execute gh pr merge and return (success, error_message)."""
    method_flag = f"--{method}"
    result = _gh([
        "pr", "merge", str(pr_number),
        method_flag, "--auto",
    ])
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "gh pr merge failed").strip()
        return False, msg
    return True, ""


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
) -> Dict[str, Any]:
    """Merge a PR and emit audit trail.

    Returns a dict with keys: success, pr_number, dispatch_id, merge_method,
    pr_title, branch, receipt_status, register_ok, error.
    """
    result: Dict[str, Any] = {
        "success": False,
        "pr_number": pr_number,
        "dispatch_id": dispatch_id,
        "merge_method": merge_method,
        "pr_title": "",
        "branch": "",
        "receipt_status": None,
        "register_ok": False,
        "error": "",
        "dry_run": dry_run,
        "overlaps": [],
    }

    # Query PR metadata before merge (needed for receipt)
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

    # Execute the merge
    ok, err = _do_merge(pr_number, merge_method)
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

    # Emit receipt to t0_receipts.ndjson
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
        result["receipt_status"] = receipt.get("append_status", "unknown")
    except Exception as exc:
        result["receipt_status"] = f"error: {exc}"
        print(f"WARN: receipt emit failed for PR #{pr_number}: {exc}", file=sys.stderr)

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
    gate, _ = _run_ci_gate(args.pr, override_reason=args.override_reason)
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

    result = merge_pr(
        pr_number=args.pr,
        dispatch_id=args.dispatch_id or "",
        merge_method=method,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    elif result["success"]:
        dry_label = " (dry-run)" if args.dry_run else ""
        print(f"OK: PR #{args.pr} merged{dry_label} via {method}")
        if result.get("receipt_status") and not args.dry_run:
            print(f"    receipt: {result['receipt_status']}")
            print(f"    register: {'ok' if result['register_ok'] else 'warn-not-written'}")
    else:
        print(f"ERROR: {result['error']}", file=sys.stderr)

    return EXIT_OK if result["success"] else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
