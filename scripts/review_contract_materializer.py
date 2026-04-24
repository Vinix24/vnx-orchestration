#!/usr/bin/env python3
"""CLI for materializing review contracts from FEATURE_PLAN and PR metadata.

Usage:
    python scripts/review_contract_materializer.py materialize \
        --pr PR-1 \
        --feature-plan FEATURE_PLAN.md \
        --pr-queue PR_QUEUE.md \
        --branch feature/my-branch \
        --changed-files "scripts/foo.py,tests/test_foo.py" \
        --dispatch-id "20260331-143522-review-contract-schema" \
        --output /path/to/contract.json

    python scripts/review_contract_materializer.py validate \
        --contract /path/to/contract.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from review_contract import (
    ReviewContract,
    TestEvidence,
    materialize_from_files,
)
from result_contract import EXIT_IO, EXIT_OK, EXIT_VALIDATION


def _parse_changed_files(value: str) -> List[str]:
    if not value:
        return []
    return sorted(set(item.strip() for item in value.split(",") if item.strip()))


def _parse_test_files(value: str) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _cmd_materialize(args: argparse.Namespace) -> int:
    feature_plan = Path(args.feature_plan)
    pr_queue = Path(args.pr_queue)

    if not feature_plan.exists():
        print(json.dumps({"ok": False, "error": f"FEATURE_PLAN not found: {feature_plan}"}))
        return EXIT_IO

    if not pr_queue.exists():
        print(json.dumps({"ok": False, "error": f"PR_QUEUE not found: {pr_queue}"}))
        return EXIT_IO

    changed_files = _parse_changed_files(args.changed_files or "")

    test_evidence = None
    if args.test_files:
        test_evidence = TestEvidence(
            test_files=_parse_test_files(args.test_files),
            test_command=args.test_command or "",
        )

    try:
        contract = materialize_from_files(
            pr_id=args.pr,
            feature_plan_path=feature_plan,
            pr_queue_path=pr_queue,
            branch=args.branch or "",
            changed_files=changed_files,
            test_evidence=test_evidence,
            dispatch_id=args.dispatch_id or "",
        )
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return EXIT_VALIDATION

    output_json = contract.to_json()

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_json + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "path": str(output_path), "content_hash": contract.content_hash}))
    else:
        print(output_json)

    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    contract_path = Path(args.contract)
    if not contract_path.exists():
        print(json.dumps({"ok": False, "error": f"contract file not found: {contract_path}"}))
        return EXIT_IO

    try:
        text = contract_path.read_text(encoding="utf-8")
        contract = ReviewContract.from_json(text)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": f"parse error: {exc}"}))
        return EXIT_VALIDATION

    errors: List[str] = []

    if not contract.pr_id:
        errors.append("missing pr_id")
    if not contract.pr_title:
        errors.append("missing pr_title")
    if not contract.deliverables:
        errors.append("missing deliverables")
    if not contract.review_stack:
        errors.append("missing review_stack")
    if not contract.risk_class:
        errors.append("missing risk_class")
    if not contract.merge_policy:
        errors.append("missing merge_policy")

    expected_hash = ReviewContract.compute_content_hash(contract.to_dict())
    if contract.content_hash and contract.content_hash != expected_hash:
        errors.append(f"content_hash mismatch: expected {expected_hash}, got {contract.content_hash}")

    if errors:
        print(json.dumps({"ok": False, "errors": errors}))
        return EXIT_VALIDATION

    print(json.dumps({"ok": True, "pr_id": contract.pr_id, "content_hash": contract.content_hash}))
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VNX review contract materializer")
    sub = parser.add_subparsers(dest="command", required=True)

    mat_parser = sub.add_parser("materialize", help="Materialize a review contract from source files")
    mat_parser.add_argument("--pr", required=True, help="PR identifier (e.g., PR-1)")
    mat_parser.add_argument("--feature-plan", required=True, help="Path to FEATURE_PLAN.md")
    mat_parser.add_argument("--pr-queue", required=True, help="Path to PR_QUEUE.md")
    mat_parser.add_argument("--branch", default="", help="Git branch name")
    mat_parser.add_argument("--changed-files", default="", help="Comma-separated changed file paths")
    mat_parser.add_argument("--test-files", default="", help="Comma-separated test file paths")
    mat_parser.add_argument("--test-command", default="", help="Test command")
    mat_parser.add_argument("--dispatch-id", default="", help="Dispatch ID")
    mat_parser.add_argument("--output", default="", help="Output file path (prints to stdout if omitted)")

    val_parser = sub.add_parser("validate", help="Validate a review contract JSON file")
    val_parser.add_argument("--contract", required=True, help="Path to contract JSON file")

    args = parser.parse_args(argv)

    if args.command == "materialize":
        return _cmd_materialize(args)
    if args.command == "validate":
        return _cmd_validate(args)

    return EXIT_VALIDATION


if __name__ == "__main__":
    raise SystemExit(main())
