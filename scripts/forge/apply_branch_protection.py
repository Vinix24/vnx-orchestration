#!/usr/bin/env python3
"""Apply scripts/forge/branch_protection.yaml to a branch's live protection
(Golf B, B1).

Reads the local YAML (default ``scripts/forge/branch_protection.yaml``),
fetches live state via ``forge_protection_drift.fetch_live_protection``, and
writes only what differs:

  - any diff among the fields the branch-protection PUT endpoint owns ->
    a single PUT with the FULL object built from the YAML (see
    ``build_put_payload`` — the PUT replaces the whole object, so a partial
    send would reset every untouched field to its GitHub default).
  - a ``required_signatures`` diff -> POST (enable) or DELETE (disable) on
    its own endpoint (not part of the PUT body at all).
  - a ``repo.allow_auto_merge`` diff -> PATCH on the repo object.
  - a ``rulesets`` diff -> never written. Checked only (see
    ``forge_protection_drift.py``'s module docstring).

No diffs at all is a no-op: idempotent, zero API writes on a second run.

Verzwak-weigering (weaken-refusal): before writing, ``is_weakening(live,
yaml)`` asks whether the YAML describes a WEAKER state than what is live
right now. A weakening apply is refused unless ``--allow-weaken "<reason>"``
is given with a non-empty reason (an empty reason is refused, never a silent
bypass) — this is the same judgment ``pr_merge.py``'s merge-door preflight
makes for a PR's own YAML edit, applied here at write time instead of at
merge time. ``--dry-run`` never writes regardless, so it is not gated by
this refusal: it always prints the built object.

Every non-dry-run invocation writes a receipt via ``governance_receipts``
from a ``finally``, not from the success path: the runs that most need a
trace are the ones that do NOT reach the end — a PUT that lands followed by
a ``required_signatures`` call that fails leaves main mutated, and a refused
weakening is the record of someone trying. The receipt carries the calls
that actually ran, the ones that never did, the verdict, the diffed fields
and the weaken reason. See ``_emit_apply_receipt``.

The FIRST apply against the real repo is an operator step, not something
this dispatch runs automatically (see the dispatch report's Open Items):

    python3 scripts/forge/apply_branch_protection.py

BILLING SAFETY: No Anthropic SDK. No direct API calls to api.anthropic.com.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent  # scripts/forge
SCRIPTS_DIR = SCRIPT_DIR.parent  # scripts/
LIB_DIR = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

from forge_protection_drift import (  # noqa: E402
    ProtectionConfig,
    ProtectionConfigError,
    ProtectionDriftError,
    bypass_allowances_to_api_object,
    compare,
    fetch_live_protection,
    is_weakening,
    load_protection_config,
    to_normalized_dict,
)
from governance_receipts import emit_governance_receipt  # noqa: E402

DEFAULT_BRANCH = "main"
DEFAULT_YAML_PATH = SCRIPT_DIR / "branch_protection.yaml"


def build_put_payload(config: ProtectionConfig) -> Dict[str, Any]:
    """The exact body for ``PUT /repos/{owner}/{repo}/branches/{branch}/protection``.

    Complete on purpose (see module docstring): the endpoint replaces the
    whole object, so every field the YAML tracks is always sent, not just
    the ones that changed.
    """
    reviews: Optional[Dict[str, Any]] = None
    if config.required_pull_request_reviews_present:
        reviews = {
            "dismiss_stale_reviews": config.dismiss_stale_reviews,
            "require_code_owner_reviews": config.require_code_owner_reviews,
            "require_last_push_approval": config.require_last_push_approval,
            "required_approving_review_count": config.required_approving_review_count,
        }
        # Only sent when the YAML grants one. The PUT replaces the whole
        # object, so an omitted key clears any grant living on the branch —
        # which is exactly what a YAML that stays silent about bypasses means.
        if config.bypass_pull_request_allowances:
            reviews["bypass_pull_request_allowances"] = bypass_allowances_to_api_object(
                config.bypass_pull_request_allowances
            )
    return {
        "required_status_checks": {
            "strict": config.strict,
            "checks": [{"context": c.context, "app_id": c.app_id} for c in config.checks],
        },
        "enforce_admins": config.enforce_admins,
        "required_pull_request_reviews": reviews,
        "restrictions": None,
        "required_linear_history": config.required_linear_history,
        "allow_force_pushes": config.allow_force_pushes,
        "allow_deletions": config.allow_deletions,
        "block_creations": config.block_creations,
        "required_conversation_resolution": config.required_conversation_resolution,
        "lock_branch": config.lock_branch,
        "allow_fork_syncing": config.allow_fork_syncing,
    }


# ---------------------------------------------------------------------------
# Write calls — isolated so tests can monkeypatch each independently without
# touching the network.
# ---------------------------------------------------------------------------


def _put_protection(
    project_root: Path, branch: str, payload: Dict[str, Any], *, gh_bin: str = "gh", timeout: int = 30,
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [
            gh_bin, "api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection",
            "--method", "PUT", "--input", "-",
        ],
        cwd=str(project_root), input=json.dumps(payload), capture_output=True, text=True, timeout=timeout,
    )


def _patch_required_signatures(
    project_root: Path, branch: str, enabled: bool, *, gh_bin: str = "gh", timeout: int = 30,
) -> "subprocess.CompletedProcess[str]":
    method = "POST" if enabled else "DELETE"
    return subprocess.run(
        [gh_bin, "api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection/required_signatures",
         "--method", method],
        cwd=str(project_root), capture_output=True, text=True, timeout=timeout,
    )


def _patch_repo_auto_merge(
    project_root: Path, allow: bool, *, gh_bin: str = "gh", timeout: int = 30,
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [gh_bin, "api", "repos/{owner}/{repo}", "--method", "PATCH",
         "-F", f"allow_auto_merge={'true' if allow else 'false'}"],
        cwd=str(project_root), capture_output=True, text=True, timeout=timeout,
    )


def _diff_buckets(diffs: List[Dict[str, Any]]) -> Dict[str, bool]:
    """Which of the three independent write surfaces a diff list touches."""
    buckets = {"protection": False, "required_signatures": False, "repo.allow_auto_merge": False}
    for d in diffs:
        field = d["field"]
        if field == "required_signatures":
            buckets["required_signatures"] = True
        elif field == "repo.allow_auto_merge":
            buckets["repo.allow_auto_merge"] = True
        elif field == "rulesets":
            continue  # checked, never written — see module docstring
        else:
            buckets["protection"] = True
    return buckets


#: The three write surfaces, in the order ``run_apply`` fires them. Used to
#: report which ones a half-finished apply never reached.
_WRITE_SURFACES = ("protection", "required_signatures", "repo.allow_auto_merge")

_RECEIPT_STATUS_BY_VERDICT = {"OK": "success", "REFUSED": "blocked"}


def _emit_apply_receipt(
    *, before: Dict[str, Any], after: Optional[Dict[str, Any]], reason: Optional[str],
    applied: bool, verdict: str, calls: List[str], pending_calls: List[str],
    diffs: List[Dict[str, Any]], weak_fields: List[str], receipts_file: Optional[str] = None,
) -> Dict[str, Any]:
    """The trace for ONE apply attempt, whatever became of it.

    ``after`` is the resulting state only where that is a claim this function
    can stand behind: the YAML on a completed apply, the untouched live state
    on a no-op or a refusal, and ``None`` on anything partial or failed —
    a half-applied branch is in a state nobody has read back, and saying
    "after = the YAML" there would put a mutation in the ledger that never
    happened.
    """
    return emit_governance_receipt(
        "branch_protection_applied",
        receipt_kind="state_mutation",
        status=_RECEIPT_STATUS_BY_VERDICT.get(verdict, "failure"),
        terminal="T0",
        source="apply_branch_protection",
        receipts_file=receipts_file,
        before=before,
        after=after,
        applied=applied,
        partial=applied and verdict != "OK",
        # NOT "verdict": append_receipt enriches every receipt with its own
        # `verdict` object (decision/reason), which would silently overwrite
        # ours -- measured on this file's own tests.
        apply_verdict=verdict,
        calls=calls,
        pending_calls=pending_calls,
        diff_fields=sorted({d["field"] for d in diffs}),
        weak_fields=weak_fields,
        weaken_reason=reason,
    )


def run_apply(
    *,
    yaml_path: Path,
    project_root: Path,
    branch: str = DEFAULT_BRANCH,
    dry_run: bool = False,
    allow_weaken_reason: Optional[str] = None,
    gh_bin: str = "gh",
    receipts_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply ``yaml_path`` to ``branch``'s live protection. Returns a result
    dict with at minimum ``verdict`` (``"OK"``, ``"DRY-RUN"``, ``"REFUSED"``,
    or ``"ERROR"``) and ``diffs``.
    """
    config = load_protection_config(yaml_path)
    yaml_norm = to_normalized_dict(config)
    live_norm = fetch_live_protection(project_root, branch=branch, gh_bin=gh_bin)
    diffs = compare(live_norm, yaml_norm)
    weakening, weak_fields = is_weakening(live_norm, yaml_norm) if diffs else (False, [])
    payload = build_put_payload(config)

    if dry_run:
        return {
            "verdict": "DRY-RUN", "applied": False, "changed": bool(diffs),
            "payload": payload, "diffs": diffs, "weak_fields": weak_fields,
        }

    # Everything from here on can mutate live protection, so everything from
    # here on is inside the try: the receipt is written in the finally, from
    # the calls that actually ran. Returning before writing it — which is
    # what a failing second call used to do — leaves a mutated branch and a
    # ledger that never heard about it.
    calls: List[str] = []
    pending: List[str] = []
    reason: Optional[str] = None
    outcome: Dict[str, Any] = {}
    try:
        if not diffs:
            outcome = {
                "verdict": "OK", "applied": False, "changed": False,
                "message": "nul wijzigingen: live branch-protection komt al overeen met de YAML",
                "diffs": [],
            }
            return outcome

        if weakening:
            reason = (allow_weaken_reason or "").strip()
            if not reason:
                outcome = {
                    "verdict": "REFUSED", "applied": False, "changed": True,
                    "message": "verzwakking geweigerd zonder --allow-weaken: " + ", ".join(weak_fields),
                    "diffs": diffs, "weak_fields": weak_fields,
                }
                return outcome

        buckets = _diff_buckets(diffs)
        pending = [surface for surface in _WRITE_SURFACES if buckets[surface]]

        if buckets["protection"]:
            proc = _put_protection(project_root, branch, payload, gh_bin=gh_bin)
            if proc.returncode != 0:
                outcome = {
                    "verdict": "ERROR", "applied": False, "diffs": diffs,
                    "message": f"PUT protection faalde: {(proc.stderr or '').strip()[:300]}",
                }
                return outcome
            calls.append("protection")
            pending.remove("protection")

        if buckets["required_signatures"]:
            proc = _patch_required_signatures(project_root, branch, config.required_signatures, gh_bin=gh_bin)
            if proc.returncode != 0:
                outcome = {
                    "verdict": "ERROR", "applied": bool(calls), "diffs": diffs,
                    "message": f"required_signatures faalde: {(proc.stderr or '').strip()[:300]}",
                }
                return outcome
            calls.append("required_signatures")
            pending.remove("required_signatures")

        if buckets["repo.allow_auto_merge"]:
            proc = _patch_repo_auto_merge(project_root, config.allow_auto_merge, gh_bin=gh_bin)
            if proc.returncode != 0:
                outcome = {
                    "verdict": "ERROR", "applied": bool(calls), "diffs": diffs,
                    "message": f"repo.allow_auto_merge faalde: {(proc.stderr or '').strip()[:300]}",
                }
                return outcome
            calls.append("repo.allow_auto_merge")
            pending.remove("repo.allow_auto_merge")

        message = f"toegepast: {', '.join(calls) if calls else 'geen schrijfacties'}"
        if reason:
            message += f" (OVERRIDE: {reason})"
        outcome = {
            "verdict": "OK", "applied": True, "changed": True, "message": message,
            "diffs": diffs, "weak_fields": weak_fields, "calls": calls,
        }
        return outcome
    finally:
        verdict = str(outcome.get("verdict") or "EXCEPTION")
        if verdict == "OK":
            after: Optional[Dict[str, Any]] = yaml_norm if calls else live_norm
        elif verdict == "REFUSED":
            after = live_norm
        else:
            after = None
        try:
            _emit_apply_receipt(
                before=live_norm, after=after, reason=reason, applied=bool(calls),
                verdict=verdict, calls=list(calls), pending_calls=list(pending),
                diffs=diffs, weak_fields=list(weak_fields), receipts_file=receipts_file,
            )
        except Exception as exc:  # receipt failure must not mask the outcome
            print(f"WAARSCHUWING: apply-receipt niet geschreven: {exc}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apply_branch_protection",
        description="Apply scripts/forge/branch_protection.yaml to a branch's live protection",
    )
    parser.add_argument("--yaml-path", default=str(DEFAULT_YAML_PATH))
    parser.add_argument("--project-root", default=str(SCRIPTS_DIR.parent))
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-weaken", default=None,
        help="Accept an apply that weakens live protection, with this required reason "
             "(empty is refused).",
    )
    parser.add_argument("--gh-bin", default="gh")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = run_apply(
            yaml_path=Path(args.yaml_path),
            project_root=Path(args.project_root),
            branch=args.branch,
            dry_run=args.dry_run,
            allow_weaken_reason=args.allow_weaken,
            gh_bin=args.gh_bin,
        )
    except (ProtectionConfigError, ProtectionDriftError) as exc:
        print(f"FOUT: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    elif result["verdict"] == "DRY-RUN":
        print(json.dumps(result["payload"], indent=2))
        if result["weak_fields"]:
            print(f"[dry-run] zou een verzwakking zijn zonder --allow-weaken: {', '.join(result['weak_fields'])}", file=sys.stderr)
    else:
        print(result.get("message", result["verdict"]))

    return 0 if result["verdict"] in ("OK", "DRY-RUN") else 1


if __name__ == "__main__":
    sys.exit(main())
