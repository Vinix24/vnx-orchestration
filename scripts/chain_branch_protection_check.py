#!/usr/bin/env python3
"""CLI path for ADR-034 §6 step 2b's branch-protection activation precondition.

A seal orchestration script runs this BEFORE calling
``chain_origin_anchor.seal_and_commit_origin`` and only passes
``branch_protection_confirmed=True`` when this exits 0. This is the caller-side
verification that check is meant to defend: ``seal_and_commit_origin`` itself
never shells out to `gh` — it only trusts an explicit caller-supplied
confirmation.

Usage:
    python3 scripts/chain_branch_protection_check.py [--project-root PATH] [--branch main] [--json]

Exit 0 when confirmed (required check present, enforce_admins on, force-pushes
blocked); exit 1 otherwise — fail-closed, same as ``verify_chain``'s "can't
check is never assume fine" contract.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from chain_origin_anchor import (  # type: ignore[import]  # noqa: E402
    ANCHOR_IMMUTABILITY_CHECK_NAME,
    check_branch_protection,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project-root", default=".", type=Path, help="repo root (default: cwd)")
    ap.add_argument("--branch", default="main", help="branch to check (default: main)")
    ap.add_argument(
        "--check-name",
        default=ANCHOR_IMMUTABILITY_CHECK_NAME,
        help="required-status-check name to look for (default: the anchor-immutability check's job name)",
    )
    ap.add_argument(
        "--owner-repo",
        default=None,
        help="explicit 'owner/repo' (default: resolved from the project's 'origin' remote)",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a human-readable line")
    args = ap.parse_args(argv)

    status = check_branch_protection(
        args.project_root.resolve(),
        args.branch,
        required_check_name=args.check_name,
        owner_repo=args.owner_repo,
    )

    if args.json:
        print(json.dumps(status.__dict__, indent=2))
    else:
        verdict = "CONFIRMED" if status.confirmed else "NOT CONFIRMED"
        print(
            f"[{verdict}] {status.owner_repo or '?'}@{status.branch}: "
            f"required_check={status.required_check_present} "
            f"enforce_admins={status.enforce_admins} "
            f"force_pushes_blocked={status.force_pushes_blocked}"
        )
        if status.reason:
            print(f"  reason: {status.reason}")

    return 0 if status.confirmed else 1


if __name__ == "__main__":
    raise SystemExit(main())
