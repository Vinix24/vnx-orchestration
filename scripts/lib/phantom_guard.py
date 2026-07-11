"""phantom_guard.py — reject a worker receipt that claims success but shows no evidence of work.

The fabrication this catches (live SEOcrawler escalation): a text-only worker (e.g. `kimi --print`)
emits a clean "GATE GREEN / done" receipt for a delivery task while changing NOTHING. The receipt
looks governed; the work is fictional.

Load-bearing signal = the WORKTREE diff, supplied EXPLICITLY by the caller. The receipt's own
provenance block is NOT trusted here: it is captured against the main repo (in_worktree=False), so
it reads empty even for a real worktree worker (the known Layer-3 gap in provenance_verification —
info-severity, diffs the wrong tree, never fires). The caller computes the diff of the actual
worktree / pushed branch and passes it in; T0's rule is "verify the pushed branch, not the report".

token_usage is only CORROBORATING: providers that report it (codex/claude) spending >0 tokens proves
an LLM ran; kimi-cli never reports tokens, so its absence/0 is not evidence of a phantom. Reviewers
legitimately produce no diff, so REVIEW_ROLES are exempt.

A read-only review can also arrive WITHOUT a REVIEW_ROLES-matching ``role`` string — e.g. a dispatch
routed through ``task_class="research_structured"`` (the fabric's review/analysis bucket, see
dispatch_router.py ROLE_TO_TASK_CLASS) or one that carries an explicit ``read_only=True`` flag on the
dispatch spec. Both are exempt the same way a REVIEW_ROLES role is: a verdict with real tokens and an
empty diff is expected, not evidence of fabrication.

Decision rule (a delivery worker is a phantom when):
    read_only is not truthy
    AND role NOT in REVIEW_ROLES
    AND task_class NOT in REVIEW_TASK_CLASSES
    AND status claims completion (done/success)
    AND the worktree diff is empty
token_usage is corroborating DETAIL only — token>0 does NOT exempt (it means an LLM thought, not
that a deliverable was produced; a delivery task that changed nothing is a phantom even if tokens
were spent). Reviews are exempt; a legitimate no-op delivery uses VNX_OVERRIDE_PHANTOM_GUARD.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

_LOG = logging.getLogger(__name__)

# SSOT for review roles — kept in sync with the kimi-routing predicate (item C). A reviewer's
# job is a verdict, not a diff, so it is never a phantom for "no changes".
REVIEW_ROLES = frozenset({"plan-reviewer", "code-reviewer", "security-reviewer", "reviewer"})

# SSOT for review/analysis task_class values — a second key onto the same exemption for callers
# that route by task_class rather than a REVIEW_ROLES-matching role string. "research_structured"
# is the fabric's canonical bucket for reviewer/architect/planner/security-engineer/data-analyst
# work (dispatch_router.py ROLE_TO_TASK_CLASS); "02_code_review" is smart_router.py's cost-routing
# classifier bucket for review/audit/lint instructions. "review"/"analysis"/"code_review" are plain
# literal fallbacks for callers that tag task_class without going through either taxonomy. The
# hyphenated variants ("code-review"/"plan-review") were dropped: they are not emitted by any real
# dispatch path (only ever a docstring example), so they widened the exemption for nothing.
REVIEW_TASK_CLASSES = frozenset({
    "research_structured", "02_code_review", "code_review", "review", "analysis",
})

# Receipt status values that assert the work completed.
COMPLETION_STATUSES = frozenset({"done", "success", "complete", "completed"})


@dataclass(frozen=True)
class PhantomVerdict:
    is_phantom: bool
    reason: str

    def __bool__(self) -> bool:  # truthy == phantom, for terse call sites
        return self.is_phantom


def _ok(reason: str) -> PhantomVerdict:
    return PhantomVerdict(is_phantom=False, reason=reason)


def phantom_guard(
    *,
    status: Optional[str],
    worktree_diff: Optional[str],
    token_usage: Optional[int] = None,
    role: Optional[str] = None,
    task_class: Optional[str] = None,
    read_only: Optional[bool] = None,
) -> PhantomVerdict:
    """Pure decision. See module docstring for the rule. worktree_diff is the REAL diff of the
    worker's worktree/branch (caller-computed), NOT the receipt's main-repo provenance."""
    if read_only:
        return _ok("read_only=True — a verdict, not a diff, is expected")
    if role is not None and role.strip().lower() in REVIEW_ROLES:
        return _ok(f"review role {role!r} — a verdict, not a diff, is expected")
    if task_class is not None and task_class.strip().lower() in REVIEW_TASK_CLASSES:
        return _ok(f"review task_class {task_class!r} — a verdict, not a diff, is expected")
    if (status or "").strip().lower() not in COMPLETION_STATUSES:
        return _ok(f"status {status!r} is not a completion claim — nothing to falsify")
    if (worktree_diff or "").strip():
        return _ok("non-empty worktree diff — work is present")
    # Empty diff on a delivery completion claim = PHANTOM, REGARDLESS of token_usage. token>0
    # means an LLM thought/read, NOT that a deliverable was produced — a delivery task that
    # changed nothing is a phantom even if tokens were spent (the earlier token>0 short-circuit
    # made the guard inert on token-reporting lanes like claude — panel P0.2 finding). Reviews
    # are exempt above; a legitimate no-op delivery uses VNX_OVERRIDE_PHANTOM_GUARD.
    tok = "0/unmeasured" if not token_usage else str(token_usage)
    return PhantomVerdict(
        is_phantom=True,
        reason=(
            f"PHANTOM: status={status!r} claims completion but the worktree diff is EMPTY "
            f"(token_usage={tok}). A delivery worker reported success with no change — the receipt "
            f"is not backed by a deliverable."
        ),
    )


def compute_branch_diff(
    head_ref: str, *, base_ref: str = "origin/main", repo: Optional[Path] = None
) -> str:
    """Diff a pushed branch against its base — T0's 'verify the pushed branch' path.

    Returns the unified diff text ('' on no changes). Raises CalledProcessError on a bad ref.
    """
    cwd = str(repo) if repo else None
    merge_base = subprocess.run(
        ["git", "merge-base", base_ref, head_ref],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return subprocess.run(
        ["git", "diff", f"{merge_base}..{head_ref}"],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout


def compute_worktree_diff(worktree_path: Path, *, base_ref: str = "origin/main") -> str:
    """Diff a worker's worktree (committed branch tip + uncommitted) against base_ref.

    Captures BOTH committed work and a still-dirty tree, so a worker that edited but did not commit
    is not misread as a phantom.
    """
    wt = str(worktree_path)
    merge_base = subprocess.run(
        ["git", "merge-base", base_ref, "HEAD"],
        cwd=wt, capture_output=True, text=True, check=True,
    ).stdout.strip()
    committed = subprocess.run(
        ["git", "diff", merge_base],  # base..working-tree (includes tracked uncommitted)
        cwd=wt, capture_output=True, text=True, check=True,
    ).stdout
    # F5 (codex): `git diff` IGNORES untracked files — a worker that creates a NEW file without
    # staging it would be false-rejected as phantom. Fold the untracked list into the diff so any
    # new file counts as real work.
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=wt, capture_output=True, text=True, check=True,
    ).stdout
    if untracked.strip():
        committed += "\n[untracked files]\n" + untracked
    return committed


def guard_receipt(
    receipt: Mapping[str, Any],
    *,
    worktree_diff: Optional[str] = None,
    worktree_path: Optional[Path] = None,
    head_ref: Optional[str] = None,
    base_ref: str = "origin/main",
) -> PhantomVerdict:
    """Run the guard against a receipt dict. Supply the diff one of three ways (precedence):
    explicit ``worktree_diff`` > ``worktree_path`` (compute_worktree_diff) > ``head_ref``
    (compute_branch_diff). If none is given, the diff is treated as empty (caller asserted no
    out-of-band evidence) — the strictest reading.
    """
    if worktree_diff is None:
        if worktree_path is not None:
            worktree_diff = compute_worktree_diff(Path(worktree_path), base_ref=base_ref)
        elif head_ref is not None:
            worktree_diff = compute_branch_diff(head_ref, base_ref=base_ref)
        else:
            worktree_diff = ""
    return phantom_guard(
        status=receipt.get("status"),
        worktree_diff=worktree_diff,
        token_usage=_extract_token_usage(receipt),
        role=receipt.get("role") or receipt.get("agent"),
        task_class=receipt.get("task_class"),
        read_only=bool(receipt.get("read_only")),
    )


def guard_at_govern(
    *,
    dispatch_id: str,
    role: Optional[str] = None,
    status: Optional[str] = None,
    token_usage: Optional[int] = None,
    worktree_path: Optional[Path] = None,
    base_sha: Optional[str] = None,
    worktree_diff: Optional[str] = None,
    task_class: Optional[str] = None,
    read_only: Optional[bool] = None,
) -> PhantomVerdict:
    """Inline govern-time phantom check for BOTH lanes (claude tmux via dispatch_govern.govern,
    providers via dispatch_envelope._govern — the kimi/glm/deepseek fabrication vector).

    Resolves the WORKER's diff in this order and NEVER raises / NEVER false-rejects:
      0. ``worktree_diff`` if supplied — a PRE-CAPTURED diff. The provider lane MUST use this: its
         worktree + dispatch/<id> branch are torn down BEFORE govern runs (codex F1), so the diff
         has to be captured while the worktree still exists and passed in here.
      1. else ``worktree_path`` if it exists (the claude tmux lane carries a LIVE worktree on
         GovernSpec — not torn down until after govern),
      2. else the dispatch's own branch ``dispatch/<sanitized id>`` (still-present isolated branch),
      3. else ABSTAIN — return ok (an unresolvable/torn-down ref must never read as "empty diff").

    Honors ``VNX_OVERRIDE_PHANTOM_GUARD=1`` (operator escape for a legitimate no-op delivery). This
    function performs NO receipt I/O — the caller appends the corrective ``failed`` receipt on a
    phantom verdict (keeps phantom_guard free of the append_receipt dependency).
    """
    import os  # noqa: PLC0415

    if os.environ.get("VNX_OVERRIDE_PHANTOM_GUARD") == "1":
        return _ok("phantom-guard overridden (VNX_OVERRIDE_PHANTOM_GUARD=1)")
    base = (base_sha or "").strip() or "origin/main"
    if worktree_diff is not None:
        diff = worktree_diff
    else:
        try:
            if worktree_path is not None and Path(worktree_path).exists():
                diff = compute_worktree_diff(Path(worktree_path), base_ref=base)
            else:
                from dispatch_worktree_isolation import _sanitize_dispatch_id  # noqa: PLC0415
                branch = f"dispatch/{_sanitize_dispatch_id(dispatch_id)}"
                diff = compute_branch_diff(branch, base_ref=base)
        except Exception as exc:  # CalledProcessError / missing ref / reaped worktree
            return _ok(f"phantom-guard ABSTAINED — worker diff unresolvable ({type(exc).__name__})")
    return phantom_guard(
        status=status, worktree_diff=diff, token_usage=token_usage, role=role,
        task_class=task_class, read_only=read_only,
    )


def record_phantom_if_any(
    *,
    dispatch_id: str,
    role: Optional[str] = None,
    status: Optional[str] = None,
    token_usage: Optional[int] = None,
    worktree_path: Optional[Path] = None,
    base_sha: Optional[str] = None,
    worktree_diff: Optional[str] = None,
    receipts_file: Optional[str] = None,
    state_dir: Optional[Path] = None,
    task_class: Optional[str] = None,
    read_only: Optional[bool] = None,
) -> PhantomVerdict:
    """``guard_at_govern`` + on a phantom verdict, append a corrective ``failed`` completion receipt.

    The corrective receipt uses ``event_type="subprocess_completion"`` (one of the watchers'
    ACTIONABLE_EVENTS — codex F3: a custom ``phantom_rejected`` event_type is invisible to the live
    trigger path) and carries ``phantom_rejected=True``, which ``dedup_completion_receipts`` honors
    as a Tier-0 override (codex F4: a same-second ``...Z`` timestamp tie would otherwise let the
    worker's ``done`` win). So the dispatch resolves FAILED while the worker's original ``done``
    receipt is PRESERVED (the contradiction is recorded, not overwritten — audit-honest, the ISAE
    lens). Never raises: a corrective-append failure is logged, not propagated.

    A phantom verdict also, best-effort, records a `blocks` fabric open-item on the
    dispatch's linked track (via gate_findings_bridge) when ``state_dir`` is supplied —
    the ppb #1039 gap: a blocking finding must surface on the track, not only the
    receipt. Degrades quietly on an unlinked dispatch; never fatal to the guard itself.
    """
    verdict = guard_at_govern(
        dispatch_id=dispatch_id, role=role, status=status, token_usage=token_usage,
        worktree_path=worktree_path, base_sha=base_sha, worktree_diff=worktree_diff,
        task_class=task_class, read_only=read_only,
    )
    if verdict.is_phantom and state_dir:
        try:
            from gate_findings_bridge import record_gate_finding  # noqa: PLC0415
            record_gate_finding(
                state_dir, dispatch_id=dispatch_id, gate_name="phantom_guard",
                summary=verdict.reason,
            )
        except Exception as exc:  # noqa: BLE001 — never break the guard on a fabric-link failure
            _LOG.warning(
                "phantom_guard: gate_findings_bridge record failed dispatch=%s: %s",
                dispatch_id, exc,
            )
    if verdict.is_phantom and receipts_file:
        try:
            import os  # noqa: PLC0415
            import sys  # noqa: PLC0415
            from datetime import datetime, timezone  # noqa: PLC0415
            # F2 (codex): append_receipt lives in scripts/, not scripts/lib — under the single-entry
            # PYTHONPATH only scripts/lib is present, so the bare import fails and is swallowed. Put
            # scripts/ on the path first (mirrors dispatch_govern.ensure_receipt).
            _scripts = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _scripts not in sys.path:
                sys.path.insert(0, _scripts)
            from append_receipt import append_receipt_payload  # noqa: PLC0415
            append_receipt_payload(
                {
                    "event_type": "subprocess_completion",
                    "dispatch_id": dispatch_id,
                    "status": "failed",
                    "phantom_rejected": True,
                    "phantom_reason": verdict.reason,
                    "source": "phantom_guard",
                    "synthesized": False,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                receipts_file=str(receipts_file),
                cache_window_seconds=0,
            )
            _LOG.warning("phantom_guard: REJECTED dispatch=%s — %s", dispatch_id, verdict.reason)
        except Exception as exc:  # noqa: BLE001 — never break govern on a corrective-append failure
            _LOG.warning("phantom_guard: corrective receipt append failed dispatch=%s: %s", dispatch_id, exc)
    return verdict


def record_guard_error(
    *,
    dispatch_id: str,
    receipts_file: Optional[str],
    error: object,
) -> None:
    """Best-effort audit signal that the phantom-guard itself errored (fail-open backstop, G4).

    The guard is the single completion chokepoint; if it raises, detection fails open. This
    appends a non-actionable ``phantom_guard_error`` receipt so the failure is recoverable from
    the ledger, not just a scrolled log line. Never raises.
    """
    _LOG.error("phantom_guard: GUARD ERROR dispatch=%s — %s", dispatch_id, error)
    if not receipts_file:
        return
    try:
        import os  # noqa: PLC0415
        import sys  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415
        _scripts = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _scripts not in sys.path:
            sys.path.insert(0, _scripts)
        from append_receipt import append_receipt_payload  # noqa: PLC0415
        append_receipt_payload(
            {
                "event_type": "phantom_guard_error",
                "dispatch_id": dispatch_id,
                "status": "guard_error",
                "guard_error": str(error),
                "source": "phantom_guard",
                "synthesized": False,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            receipts_file=str(receipts_file),
            cache_window_seconds=0,
        )
    except Exception as exc:  # noqa: BLE001 — never break the caller on an audit-append failure
        _LOG.error("phantom_guard: guard-error signal append failed dispatch=%s: %s", dispatch_id, exc)


def _extract_token_usage(receipt: Mapping[str, Any]) -> Optional[int]:
    """Best-effort total token count from a receipt; None when unmeasured (e.g. kimi-cli)."""
    tu = receipt.get("token_usage")
    if isinstance(tu, Mapping):
        total = sum(int(tu.get(k, 0) or 0) for k in ("input", "output", "total"))
        return total or None
    if isinstance(tu, (int, float)):
        return int(tu) or None
    return None


def main(argv: Optional[list] = None) -> int:
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(description="VNX phantom-guard: reject evidence-free GATE-GREEN receipts")
    p.add_argument("--receipt-json", help="Receipt as a JSON string (or use --status/--role).")
    p.add_argument("--status", help="Receipt status (when not passing --receipt-json).")
    p.add_argument("--role", default=None)
    p.add_argument("--task-class", default=None)
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--token-usage", type=int, default=None)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--worktree", help="Path to the worker's worktree (diff it vs --base).")
    src.add_argument("--branch", help="Pushed branch ref to diff vs --base.")
    p.add_argument("--base", default="origin/main")
    args = p.parse_args(argv)

    if args.receipt_json:
        receipt = json.loads(args.receipt_json)
        verdict = guard_receipt(
            receipt,
            worktree_path=Path(args.worktree) if args.worktree else None,
            head_ref=args.branch,
            base_ref=args.base,
        )
    else:
        diff = ""
        if args.worktree:
            diff = compute_worktree_diff(Path(args.worktree), base_ref=args.base)
        elif args.branch:
            diff = compute_branch_diff(args.branch, base_ref=args.base)
        verdict = phantom_guard(
            status=args.status, worktree_diff=diff,
            token_usage=args.token_usage, role=args.role,
            task_class=args.task_class, read_only=args.read_only,
        )

    print(verdict.reason, file=sys.stderr)
    print(json.dumps({"is_phantom": verdict.is_phantom, "reason": verdict.reason}))
    return 1 if verdict.is_phantom else 0


if __name__ == "__main__":
    raise SystemExit(main())
