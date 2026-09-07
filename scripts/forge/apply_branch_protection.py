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

Every non-dry-run invocation writes a receipt (before/after normalized
state, the weaken reason if one was used) via ``governance_receipts`` — see
``_emit_apply_receipt``.

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
    return {
        "required_status_checks": {
            "strict": config.strict,
            "checks": [{"context": c.context, "app_id": c.app_id} for c in config.checks],
        },
        "enforce_admins": config.enforce_admins,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": config.dismiss_stale_reviews,
            "require_code_owner_reviews": config.require_code_owner_reviews,
            "require_last_push_approval": config.require_last_push_approval,
            "required_approving_review_count": config.required_approving_review_count,
        },
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


def _emit_apply_receipt(
    *, before: Dict[str, Any], after: Dict[str, Any], reason: Optional[str], applied: bool,
    receipts_file: Optional[str] = None,
) -> Dict[str, Any]:
    return emit_governance_receipt(
        "branch_protection_applied",
        receipt_kind="state_mutation",
        status="success",
        terminal="T0",
        source="apply_branch_protection",
        receipts_file=receipts_file,
        before=before,
        after=after,
        applied=applied,
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

    if not diffs:
        _emit_apply_receipt(
            before=live_norm, after=live_norm, reason=None, applied=False, receipts_file=receipts_file,
        )
        return {
            "verdict": "OK", "applied": False, "changed": False,
            "message": "nul wijzigingen: live branch-protection komt al overeen met de YAML",
            "diffs": [],
        }

    reason: Optional[str] = None
    if weakening:
        reason = (allow_weaken_reason or "").strip()
        if not reason:
            return {
                "verdict": "REFUSED", "applied": False, "changed": True,
                "message": "verzwakking geweigerd zonder --allow-weaken: " + ", ".join(weak_fields),
                "diffs": diffs, "weak_fields": weak_fields,
            }

    buckets = _diff_buckets(diffs)
    calls: List[str] = []

    if buckets["protection"]:
        proc = _put_protection(project_root, branch, payload, gh_bin=gh_bin)
        if proc.returncode != 0:
            return {
                "verdict": "ERROR", "applied": False, "diffs": diffs,
                "message": f"PUT protection faalde: {(proc.stderr or '').strip()[:300]}",
            }
        calls.append("protection")

    if buckets["required_signatures"]:
        proc = _patch_required_signatures(project_root, branch, config.required_signatures, gh_bin=gh_bin)
        if proc.returncode != 0:
            return {
                "verdict": "ERROR", "applied": bool(calls), "diffs": diffs,
                "message": f"required_signatures faalde: {(proc.stderr or '').strip()[:300]}",
            }
        calls.append("required_signatures")

    if buckets["repo.allow_auto_merge"]:
        proc = _patch_repo_auto_merge(project_root, config.allow_auto_merge, gh_bin=gh_bin)
        if proc.returncode != 0:
            return {
                "verdict": "ERROR", "applied": bool(calls), "diffs": diffs,
                "message": f"repo.allow_auto_merge faalde: {(proc.stderr or '').strip()[:300]}",
            }
        calls.append("repo.allow_auto_merge")

    _emit_apply_receipt(before=live_norm, after=yaml_norm, reason=reason, applied=True, receipts_file=receipts_file)

    message = f"toegepast: {', '.join(calls) if calls else 'geen schrijfacties'}"
    if reason:
        message += f" (OVERRIDE: {reason})"
    return {
        "verdict": "OK", "applied": True, "changed": True, "message": message,
        "diffs": diffs, "weak_fields": weak_fields, "calls": calls,
    }


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
